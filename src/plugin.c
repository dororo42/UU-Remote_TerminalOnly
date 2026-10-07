/* Private source candidate. No production broker, clipboard provider or DLL hook. */
#include "freerdp-adapter.h"
#include "uurb_ready.h"
#include <bcrypt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
static const char module_anchor;
static SRWLOCK generations=SRWLOCK_INIT;
static void* generation_owner;
static uint64_t generation_serial;
static volatile LONG last_generation_uncertain;
typedef struct {
 IWTSPlugin iface; ui_exports exports; rdpContext* context; ui_core core;
 ui_geometry geometry; ui_live snapshot; SRWLOCK lock;
 FreeRDP_TimerID timer; HANDLE stop,worker,pipe; HMODULE module;
 volatile LONG terminating,close_requested,quarantined,reap_started; uint64_t generation;
 DWORD peer_pid; uint64_t peer_start; uint8_t bootstrap[32],secret[32];
 char pipe_name[160]; bool authenticated,timer_quiet,worker_quiet,previous_uncertain;
} Plugin;
static uint64_t clock_ns(void* data) {
 (void)data;LARGE_INTEGER value,frequency;
 if(!QueryPerformanceFrequency(&frequency)||!QueryPerformanceCounter(&value)||frequency.QuadPart<=0){ return UINT64_MAX; }
 uint64_t ticks=(uint64_t)value.QuadPart,rate=(uint64_t)frequency.QuadPart;
 return (ticks/rate)*UINT64_C(1000000000)+(ticks%rate)*UINT64_C(1000000000)/rate;
}
static uint64_t creation(HANDLE process) {
 FILETIME a,b,c,d;if(!GetProcessTimes(process,&a,&b,&c,&d)){ return 0; }
 return (uint64_t)a.dwLowDateTime|((uint64_t)a.dwHighDateTime<<32);
}
static bool decimal_env(const char* key,uint64_t* out) {
 char text[32];DWORD n=GetEnvironmentVariableA(key,text,sizeof(text));
 if(!n||n>=sizeof(text)){ return false; }
 for(DWORD i=0;i<n;i++){ if(text[i]<'0'||text[i]>'9'){ return false; } }
 errno=0;char* end=NULL;unsigned long long v=strtoull(text,&end,10);
 if(errno==ERANGE||!end||*end){ return false; }*out=(uint64_t)v;return true;
}
static bool bootstrap_env(uint8_t out[32]) {
 char text[65];if(GetEnvironmentVariableA("UURB_FULL_BOOTSTRAP",text,sizeof(text))!=64){ return false; }
 for(unsigned i=0;i<32;i++){
  unsigned x=0;for(unsigned j=0;j<2;j++){char ch=text[i*2+j];unsigned value;
   if(ch>='0'&&ch<='9'){ value=(unsigned)(ch-'0'); }else if(ch>='a'&&ch<='f'){ value=(unsigned)(ch-'a'+10); }else return false;
   x=(x<<4)|value;}out[i]=(uint8_t)x;
 }
 SecureZeroMemory(text,sizeof(text));return true;
}
static bool same_peer(Plugin* p) {
 ULONG pid=0;if(!GetNamedPipeClientProcessId(p->pipe,&pid)||pid!=p->peer_pid){ return false; }
 HANDLE process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,pid);
 if(!process){ return false; }uint64_t started=creation(process);CloseHandle(process);
 return started && started==p->peer_start;
}
static bool live(void* data,ui_live* out) {
 Plugin* p=data;
 if(InterlockedCompareExchange(&p->terminating,0,0)){ return false; }
 if(!ui_rdp_live(&p->exports,p->context,out,&p->geometry)){ return false; }
 if(out->geometry.width!=p->geometry.width||out->geometry.height!=p->geometry.height){
  if(p->geometry.epoch==UINT64_MAX){ return false; }
  p->geometry.width=out->geometry.width;p->geometry.height=out->geometry.height;p->geometry.epoch++;
  /* No implicit transform reinterpretation. Geometry acknowledgement is stale. */
  out->geometry=p->geometry;p->core.geometry=p->geometry;
 }
 p->snapshot=*out;return true;
}
static bool send_primitive(void* data,const ui_primitive* event) {
 Plugin* p=data;
 if(InterlockedCompareExchange(&p->terminating,0,0)){ return false; }
 return ui_rdp_send(&p->exports,p->context,event);
}
static uint64_t tick(rdpContext* context,void* data,FreeRDP_TimerID id,uint64_t stamp,uint64_t interval) {
 Plugin* p=data;(void)id;(void)stamp;
 /* Closing callbacks must never touch a freed core context, even when remove fails. */
 if(InterlockedCompareExchange(&p->terminating,0,0)){ return 0; }
 if(!TryAcquireSRWLockExclusive(&p->lock)){ return interval; }
 if(InterlockedCompareExchange(&p->terminating,0,0)||context!=p->context){ReleaseSRWLockExclusive(&p->lock);return 0;}
 ui_live state;memset(&state,0,sizeof(state));bool valid=live(p,&state);
 if(InterlockedCompareExchange(&p->close_requested,0,0)){ ui_close(&p->core,valid&&state.active&&!state.suspended&&state.associated); }
 ui_slice(&p->core);
 bool finished=ui_locally_drained(&p->core);
 ReleaseSRWLockExclusive(&p->lock);
 /* State is retained until Terminated proves remove + worker join, never freed here. */
 return finished?0:interval;
}
static bool check_stop(Plugin* p,uint64_t deadline) {
 return clock_ns(NULL)<deadline && WaitForSingleObject(p->stop,0)==WAIT_TIMEOUT;
}
static bool wait_stop(Plugin* p,uint64_t deadline) {
 if(!check_stop(p,deadline)){ return false; }
 return WaitForSingleObject(p->stop,5)==WAIT_TIMEOUT && clock_ns(NULL)<deadline;
}
static bool read_exact(Plugin* p,uint8_t* bytes,DWORD count,uint64_t deadline) {
 DWORD used=0;
 while(used<count){
  if(!check_stop(p,deadline)||!same_peer(p)){ return false; }
  DWORD available=0,got=0;
  if(!PeekNamedPipe(p->pipe,NULL,0,NULL,&available,NULL)){ return false; }
  if(!available){if(!wait_stop(p,deadline)){ return false; }continue;}
  if(clock_ns(NULL)>=deadline){ return false; }
  DWORD amount=available<count-used?available:count-used;
  if(!ReadFile(p->pipe,bytes+used,amount,&got,NULL)){
   if(GetLastError()!=ERROR_NO_DATA||!wait_stop(p,deadline)){ return false; }
   continue;
  }
  if(!got){ return false; }
  used+=got;
 }
 return clock_ns(NULL)<deadline;
}
static bool write_exact(Plugin* p,const uint8_t* bytes,DWORD count,uint64_t deadline) {
 DWORD used=0;
 while(used<count){
  if(!check_stop(p,deadline)||!same_peer(p)||clock_ns(NULL)>=deadline){ return false; }
  DWORD sent=0;
  if(!WriteFile(p->pipe,bytes+used,count-used,&sent,NULL)){
   DWORD error=GetLastError();if(error!=ERROR_NO_DATA||!wait_stop(p,deadline)){ return false; }
  }else if(!sent && !wait_stop(p,deadline)){ return false; }
  used+=sent;
 }
 return clock_ns(NULL)<deadline;
}
static void capability_reply(Plugin* p,uint8_t out[144]) {
 memset(out,0,144);ui_put32(out,UI_MAGIC);ui_put32(out+4,UI_VERSION);ui_put32(out+8,144);
 ui_put32(out+12,p->snapshot.active&&!p->snapshot.suspended?UI_OK:UI_INACTIVE);
 ui_put64(out+16,p->generation);memcpy(out+40,p->secret,32);
 ui_geometry* g=&p->geometry;
 ui_put64(out+72,g->epoch);ui_put32(out+80,g->width);ui_put32(out+84,g->height);
 ui_put32(out+88,g->dpi);ui_put32(out+92,g->scale_n);ui_put32(out+96,g->scale_d);
 ui_put32(out+100,(uint32_t)g->origin_x);ui_put32(out+104,(uint32_t)g->origin_y);
 ui_put32(out+108,(uint32_t)g->view_x);ui_put32(out+112,(uint32_t)g->view_y);
 ui_put32(out+116,g->view_w);ui_put32(out+120,g->view_h);ui_put32(out+128,p->snapshot.caps);ui_put32(out+136,p->previous_uncertain?1u:0u);
}
static bool hello(Plugin* p) {
 uint8_t bytes[56];uint64_t until=clock_ns(NULL)+UINT64_C(1000000000);
 if(!read_exact(p,bytes,sizeof(bytes),until)||ui_get32(bytes)!=UI_MAGIC||ui_get32(bytes+4)!=UI_VERSION||
 ui_get32(bytes+8)!=p->peer_pid||ui_get32(bytes+12)||ui_get64(bytes+16)!=p->peer_start){ return false; }
 unsigned mismatch=0;for(unsigned i=0;i<32;i++){ mismatch|=(unsigned)(p->bootstrap[i]^bytes[24+i]); }
 SecureZeroMemory(bytes,sizeof(bytes));if(mismatch){ return false; }
 uint8_t reply[144];AcquireSRWLockExclusive(&p->lock);capability_reply(p,reply);ReleaseSRWLockExclusive(&p->lock);
 bool okay=write_exact(p,reply,sizeof(reply),until);SecureZeroMemory(reply,sizeof(reply));return okay;
}
static DWORD WINAPI reader(void* data) {
 Plugin* p=data;uint8_t* wire=malloc(UI_WIRE_MAX);
 if(!wire){InterlockedExchange(&p->close_requested,1);return 1;}
 /* Only the authenticated exact process may acquire input ownership. */
 while(WaitForSingleObject(p->stop,5)==WAIT_TIMEOUT){
  BOOL result=ConnectNamedPipe(p->pipe,NULL);DWORD error=result?ERROR_SUCCESS:GetLastError();
  if(result||error==ERROR_PIPE_LISTENING){ continue; }
  if(error==ERROR_NO_DATA){(void)DisconnectNamedPipe(p->pipe);continue;}
  if(error!=ERROR_PIPE_CONNECTED){ goto close; }
  if(!same_peer(p)||!hello(p)){(void)DisconnectNamedPipe(p->pipe);continue;}
  p->authenticated=true;break;
 }
 if(!p->authenticated){ goto close; }
 while(WaitForSingleObject(p->stop,0)==WAIT_TIMEOUT){
  /* Drain output without holding the core lock across I/O. */
  ui_receipt receipt;memset(&receipt,0,sizeof(receipt));bool found;
  AcquireSRWLockExclusive(&p->lock);found=ui_take_receipt(&p->core,&receipt);ReleaseSRWLockExclusive(&p->lock);
  if(found){uint8_t answer[UI_RECEIPT_BYTES];ui_encode_receipt(answer,&receipt);
   if(!write_exact(p,answer,sizeof(answer),clock_ns(NULL)+UINT64_C(1000000000))){ goto close; }
   continue;
  }
  DWORD available=0;
  if(!same_peer(p)||!PeekNamedPipe(p->pipe,NULL,0,NULL,&available,NULL)){ goto close; }
  if(!available){if(!wait_stop(p,clock_ns(NULL)+UINT64_C(1000000000))){ goto close; }continue;}
  uint64_t deadline=clock_ns(NULL)+UINT64_C(1000000000);
  if(!read_exact(p,wire,12,deadline)){ goto close; }
  DWORD bytes=ui_get32(wire+8);
  if(bytes<UI_HEADER||bytes>UI_WIRE_MAX||!read_exact(p,wire+12,bytes-12,deadline)){ goto close; }
  AcquireSRWLockExclusive(&p->lock);
  enum ui_status status=InterlockedCompareExchange(&p->close_requested,0,0)?UI_CLOSED:ui_accept(&p->core,wire,bytes,&receipt);
  if(status==UI_OK && ui_get32(wire+12)!=0){
   p->geometry=p->core.geometry;uint8_t cap[144];capability_reply(p,cap);
   ReleaseSRWLockExclusive(&p->lock);SecureZeroMemory(wire,bytes);
   bool sent=write_exact(p,cap,sizeof(cap),deadline);SecureZeroMemory(cap,sizeof(cap));
   if(!sent){ goto close; }continue;
  }
  ReleaseSRWLockExclusive(&p->lock);SecureZeroMemory(wire,bytes);
  if(status!=UI_OK){receipt.status=status;uint8_t answer[UI_RECEIPT_BYTES];ui_encode_receipt(answer,&receipt);
   if(!write_exact(p,answer,sizeof(answer),deadline)){ goto close; }
   if(status==UI_AUTH||status==UI_REPLAY||status==UI_BAD_WIRE){ goto close; }
  }
 }
close:
 SecureZeroMemory(wire,UI_WIRE_MAX);free(wire);
 InterlockedExchange(&p->close_requested,1);
 /* Reader does not remove the timer or dereference rdpContext. */
 (void)DisconnectNamedPipe(p->pipe);
 return 0;
}
static UINT terminate(IWTSPlugin* iface);
static UINT initialize_failure(Plugin* p,DWORD error){
 InterlockedExchange(&p->terminating,1);
 if(p->stop){ (void)SetEvent(p->stop); }
 /* RegisterPlugin already owns iface. Do not free it during Initialize failure.
  * Retain the process-generation barrier until actual Terminated quiescence. */
 if(p->timer && p->exports.timer_remove(p->context,p->timer)){ p->timer=0; }
 return error;
}
static UINT initialize(IWTSPlugin* iface,IWTSVirtualChannelManager* manager) {
 Plugin* p=(Plugin*)iface;(void)manager;
 p->pipe=CreateNamedPipeA(p->pipe_name,PIPE_ACCESS_DUPLEX|FILE_FLAG_FIRST_PIPE_INSTANCE,
 PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_NOWAIT|PIPE_REJECT_REMOTE_CLIENTS,1,65536,65536,0,NULL);
 if(p->pipe==INVALID_HANDLE_VALUE){DWORD error=GetLastError();return initialize_failure(p,error);}
 p->stop=CreateEventW(NULL,TRUE,FALSE,NULL);
 if(!p->stop){DWORD error=GetLastError();return initialize_failure(p,error);}
 p->timer=p->exports.timer_add(p->context,UINT64_C(1000000),tick,p,true);
 if(!p->timer){return initialize_failure(p,ERROR_NOT_READY);}
 p->worker=CreateThread(NULL,0,reader,p,0,NULL);
 if(!p->worker){DWORD error=GetLastError();return initialize_failure(p,error);}
 if(!uurb_ready_publish(L"UURB_FULL_SDL_READY","sdl")){return initialize_failure(p,ERROR_NOT_READY);}
 return ERROR_SUCCESS;
}
static UINT attached(IWTSPlugin* iface){
 Plugin* p=(Plugin*)iface;
 /* The generation is never reopened after closing; reconnect creates a new object. */
 return InterlockedCompareExchange(&p->close_requested,0,0)?ERROR_INVALID_STATE:ERROR_SUCCESS;
}
static UINT detached(IWTSPlugin* iface){
 Plugin* p=(Plugin*)iface;InterlockedExchange(&p->close_requested,1);
 return ERROR_SUCCESS;
}
static DWORD quarantine_generation(Plugin* p,const char* reason){
 /* Single retained generation, no reconnect allocation growth. Supervisor must
  * replace only this verified owned SDL process after bounded drain fails. */
 InterlockedExchange(&p->quarantined,1);
 fprintf(stderr,"UURB full quarantine generation=%llu reason=%s supervisor-replace-owned-sdl=1\n",
  (unsigned long long)p->generation,reason);
 return 1;
}
static DWORD WINAPI reap_generation(void* data){
 Plugin* p=data;
 /* Off main loop only. Timer removal cannot be retried after context revocation:
  * core owns that pointer and may already be tearing it down. */
 if(!p->timer_quiet){return quarantine_generation(p,"timer-not-quiesced");}
 ULONGLONG deadline=GetTickCount64()+3000;
 bool locked=false;
 for(unsigned attempt=0;attempt<64 && GetTickCount64()<deadline;attempt++){
  DWORD waited=p->worker?WaitForSingleObject(p->worker,25):WAIT_OBJECT_0;
  if(waited==WAIT_FAILED){return quarantine_generation(p,"worker-wait-failed");}
  if(waited==WAIT_OBJECT_0 && TryAcquireSRWLockExclusive(&p->lock)){locked=true;break;}
  Sleep(25);
 }
 if(!locked){return quarantine_generation(p,"worker-or-lock-timeout");}
 ui_close(&p->core,false);
 ui_mark_quiesced(&p->core,true,true);
 if(p->core.phase!=UI_QUIESCED){ReleaseSRWLockExclusive(&p->lock);return quarantine_generation(p,"core-not-quiesced");}
 bool uncertain=p->core.remote_uncertain;uint64_t longest=p->core.longest_call_ns;
 ui_destroy(&p->core);ReleaseSRWLockExclusive(&p->lock);
 fprintf(stderr,"UURB full generation=%llu quiesced remote-uncertain=%u longest-wrapper-ns=%llu\n",
 (unsigned long long)p->generation,uncertain?1u:0u,(unsigned long long)longest);
 if(p->worker){CloseHandle(p->worker);}
 if(p->stop){CloseHandle(p->stop);}
 if(p->pipe!=INVALID_HANDLE_VALUE){CloseHandle(p->pipe);}
 AcquireSRWLockExclusive(&generations);
 if(generation_owner==p){InterlockedExchange(&last_generation_uncertain,uncertain?1:0);generation_owner=NULL;}
 ReleaseSRWLockExclusive(&generations);
 SecureZeroMemory(p->bootstrap,sizeof(p->bootstrap));SecureZeroMemory(p->secret,sizeof(p->secret));free(p);
 return 0;
}
static UINT terminate(IWTSPlugin* iface){
 Plugin* p=(Plugin*)iface;
 if(InterlockedCompareExchange(&p->reap_started,1,0)){return ERROR_INVALID_STATE;}
 InterlockedExchange(&p->terminating,1);InterlockedExchange(&p->close_requested,1);
 if(p->stop){(void)SetEvent(p->stop);}
 if(p->worker){(void)CancelSynchronousIo(p->worker);}
 if(p->pipe!=INVALID_HANDLE_VALUE){(void)CancelIoEx(p->pipe,NULL);}
 /* This callback may run on the core main loop. Never join the worker here.
  * remove marks the timer disabled under the actual timer entries lock.
  * Failure is NOT a teardown veto: callbacks are revoked and pinned state stays. */
 p->timer_quiet=!p->timer || p->exports.timer_remove(p->context,p->timer);
 p->context=NULL; /* No core/channel/entry-point pointer survives into reaper. */
 HANDLE reaper=CreateThread(NULL,0,reap_generation,p,0,NULL);
 if(!reaper){return quarantine_generation(p,"reaper-create-failed");}
 CloseHandle(reaper); /* Windows retains its executing thread; module is PINned. */
 return ERROR_SUCCESS;
}
__declspec(dllexport) UINT VCAPITYPE DVCPluginEntry(IDRDYNVC_ENTRY_POINTS* entries){
 if(!entries||!entries->GetRdpContext||!entries->RegisterPlugin||!entries->GetPlugin){ return ERROR_INVALID_PARAMETER; }
 if(entries->GetPlugin(entries,"uurb-full-input")){ return ERROR_ALREADY_EXISTS; }
 Plugin* p=calloc(1,sizeof(*p));if(!p){ return ERROR_NOT_ENOUGH_MEMORY; }
 p->pipe=INVALID_HANDLE_VALUE;InitializeSRWLock(&p->lock);
 p->context=entries->GetRdpContext(entries);
 uint64_t peer_pid;
 DWORD len=GetEnvironmentVariableA("UURB_FULL_PIPE",p->pipe_name,sizeof(p->pipe_name));
 const char prefix[]="\\\\.\\pipe\\uurb-full-";
 if(!p->context||!p->context->settings||!ui_resolve(&p->exports,GetModuleHandleA(NULL))||!len||len>=sizeof(p->pipe_name)||
 strncmp(p->pipe_name,prefix,sizeof(prefix)-1)||!bootstrap_env(p->bootstrap)||
 !decimal_env("UURB_FULL_PEER_PID",&peer_pid)||!peer_pid||peer_pid>UINT32_MAX||
 !decimal_env("UURB_FULL_PEER_START",&p->peer_start)||!p->peer_start){free(p);return ERROR_INVALID_PARAMETER;}
 p->peer_pid=(DWORD)peer_pid;
 if(!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,&module_anchor,&p->module)||
 BCryptGenRandom(NULL,p->secret,sizeof(p->secret),BCRYPT_USE_SYSTEM_PREFERRED_RNG)!=0){free(p);return ERROR_NOT_READY;}
 char module_path[MAX_PATH];
 DWORD module_length=GetModuleFileNameA(p->module,module_path,sizeof(module_path));
 if(!module_length||module_length>=sizeof(module_path)){free(p);return ERROR_INVALID_NAME;}
 const char* base=strrchr(module_path,'\\');base=base?base+1:module_path;
 if(strcmp(base,"uurb-full-input-client.dll")||
 GetModuleHandleA("uurb-full-input-client.dll")!=p->module){free(p);return ERROR_INVALID_NAME;}
 AcquireSRWLockExclusive(&generations);
 if(generation_owner||generation_serial==UINT64_MAX){
  UINT status=generation_owner && InterlockedCompareExchange(&((Plugin*)generation_owner)->quarantined,0,0)?ERROR_NOT_READY:ERROR_ALREADY_EXISTS;
  ReleaseSRWLockExclusive(&generations);free(p);return status;
 }
 p->generation=++generation_serial;p->previous_uncertain=InterlockedCompareExchange(&last_generation_uncertain,0,0)!=0;generation_owner=p;ReleaseSRWLockExclusive(&generations);
 p->geometry=(ui_geometry){1,p->exports.integer(p->context->settings,FreeRDP_DesktopWidth),
 p->exports.integer(p->context->settings,FreeRDP_DesktopHeight),96,1,1,0,0,0,0,0,0};
 p->geometry.view_w=p->geometry.width;p->geometry.view_h=p->geometry.height;
 ui_backend backend={p,clock_ns,live,send_primitive,NULL};
 ui_init(&p->core,&backend,p->generation,p->secret,&p->geometry,false);
 p->iface.Initialize=initialize;p->iface.Attached=attached;p->iface.Detached=detached;p->iface.Terminated=terminate;
 UINT result=entries->RegisterPlugin(entries,"uurb-full-input",&p->iface);
 if(result!=ERROR_SUCCESS){
  AcquireSRWLockExclusive(&generations);if(generation_owner==p){ generation_owner=NULL; }ReleaseSRWLockExclusive(&generations);
  SecureZeroMemory(p,sizeof(*p));free(p);
 }
 return result;
}
