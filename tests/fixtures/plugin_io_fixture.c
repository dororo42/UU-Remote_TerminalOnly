#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t DWORD;
typedef int HANDLE;
typedef struct {HANDLE stop,pipe;} Plugin;
#define WAIT_TIMEOUT 258u
#define ERROR_NO_DATA 232u
static uint64_t stamp;
static unsigned idle_waits,reads,writes,empty_peeks,read_no_data,write_no_data,zero_writes,chunk;
static bool stopped,stop_on_idle,peer_ok,peer_expires;
static DWORD last_error;
static uint64_t clock_ns(void* unused){(void)unused;return stamp;}
static DWORD WaitForSingleObject(HANDLE handle,DWORD timeout){
 (void)handle;if(stopped){return 0;}if(timeout){idle_waits++;if(stop_on_idle){stopped=true;return 0;}stamp+=(uint64_t)timeout*1000000;}
 return WAIT_TIMEOUT;
}
static bool same_peer(Plugin* p){(void)p;if(peer_expires){stamp=1000000000;}return peer_ok;}
static bool PeekNamedPipe(HANDLE p,void* a,DWORD b,void* c,DWORD* available,void* d){
 (void)p;(void)a;(void)b;(void)c;(void)d;if(empty_peeks){empty_peeks--;*available=0;}else *available=64;return true;
}
static bool ReadFile(HANDLE p,void* bytes,DWORD amount,DWORD* got,void* ov){
 (void)p;(void)ov;reads++;*got=0;if(read_no_data){read_no_data--;last_error=ERROR_NO_DATA;return false;}
 *got=amount<chunk?amount:chunk;memset(bytes,0x5a,*got);return true;
}
static bool WriteFile(HANDLE p,const void* bytes,DWORD amount,DWORD* got,void* ov){
 (void)p;(void)bytes;(void)ov;writes++;*got=0;if(write_no_data){write_no_data--;last_error=ERROR_NO_DATA;return false;}
 if(zero_writes){zero_writes--;return true;}*got=amount<chunk?amount:chunk;return true;
}
static DWORD GetLastError(void){return last_error;}
#ifndef IO_FUNCTIONS
#define IO_FUNCTIONS "io-functions.inc"
#endif
#include IO_FUNCTIONS
static void reset(void){stamp=10;idle_waits=reads=writes=empty_peeks=read_no_data=write_no_data=zero_writes=0;chunk=64;stopped=stop_on_idle=peer_expires=false;peer_ok=true;last_error=0;}
#define CHECK(expr) do {if(!(expr)){fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#expr);return 1;}}while(0)
int main(void){
 Plugin p={1,2};uint8_t bytes[12]={0};unsigned passed=0;
 reset();CHECK(read_exact(&p,bytes,sizeof(bytes),100000000));CHECK(idle_waits==0&&reads==1&&stamp==10&&bytes[11]==0x5a);passed++;
 reset();CHECK(write_exact(&p,bytes,sizeof(bytes),100000000));CHECK(idle_waits==0&&writes==1&&stamp==10);passed++;
 reset();chunk=3;CHECK(read_exact(&p,bytes,sizeof(bytes),100000000));CHECK(reads==4&&idle_waits==0);passed++;
 reset();chunk=3;CHECK(write_exact(&p,bytes,sizeof(bytes),100000000));CHECK(writes==4&&idle_waits==0);passed++;
 reset();empty_peeks=1;CHECK(read_exact(&p,bytes,sizeof(bytes),100000000));CHECK(idle_waits==1&&reads==1&&stamp==5000010);passed++;
 reset();read_no_data=1;CHECK(read_exact(&p,bytes,sizeof(bytes),100000000));CHECK(idle_waits==1&&reads==2);passed++;
 reset();write_no_data=1;CHECK(write_exact(&p,bytes,sizeof(bytes),100000000));CHECK(idle_waits==1&&writes==2);passed++;
 reset();zero_writes=1;CHECK(write_exact(&p,bytes,sizeof(bytes),100000000));CHECK(idle_waits==1&&writes==2);passed++;
 reset();stopped=true;CHECK(!read_exact(&p,bytes,sizeof(bytes),100000000));CHECK(reads==0&&idle_waits==0);CHECK(!write_exact(&p,bytes,sizeof(bytes),100000000)&&writes==0);passed++;
 reset();empty_peeks=1;stop_on_idle=true;CHECK(!read_exact(&p,bytes,sizeof(bytes),100000000));CHECK(reads==0&&idle_waits==1);passed++;
 reset();CHECK(!read_exact(&p,bytes,sizeof(bytes),10)&&reads==0&&idle_waits==0);CHECK(!write_exact(&p,bytes,sizeof(bytes),10)&&writes==0);passed++;
 reset();empty_peeks=100;CHECK(!read_exact(&p,bytes,sizeof(bytes),4000000));CHECK(reads==0&&idle_waits==1);passed++;
 reset();write_no_data=100;CHECK(!write_exact(&p,bytes,sizeof(bytes),4000000));CHECK(writes==1&&idle_waits==1);passed++;
 reset();peer_ok=false;CHECK(!read_exact(&p,bytes,sizeof(bytes),100000000)&&reads==0);CHECK(!write_exact(&p,bytes,sizeof(bytes),100000000)&&writes==0);passed++;
 reset();peer_expires=true;CHECK(!read_exact(&p,bytes,sizeof(bytes),100000000)&&reads==0);stamp=10;CHECK(!write_exact(&p,bytes,sizeof(bytes),100000000)&&writes==0);passed++;
 printf("PASS %u ready/noData/partial/stop/deadline/peer cases; fake callbacks only\n",passed);return 0;
}
