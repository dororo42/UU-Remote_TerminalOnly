#define WIN32_LEAN_AND_MEAN
#include "uurb_ready.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
uint64_t uurb_creation(HANDLE p){FILETIME a,b,c,d;if(!GetProcessTimes(p,&a,&b,&c,&d)){return 0;}return a.dwLowDateTime|((uint64_t)a.dwHighDateTime<<32);}
static BOOL nonce(char out[65]){
 if(GetEnvironmentVariableA("UURB_FULL_READY_NONCE",out,65)!=64){return FALSE;}
 for(unsigned i=0;i<64;i++){if(!((out[i]>='0'&&out[i]<='9')||(out[i]>='a'&&out[i]<='f'))){return FALSE;}}
 return TRUE;
}
static BOOL path(const wchar_t* key,wchar_t out[MAX_PATH]){
 DWORD n=GetEnvironmentVariableW(key,out,MAX_PATH);
 return n>3 && n<MAX_PATH && out[0]==L'C' && out[1]==L':' && out[2]==L'\\';
}
BOOL uurb_ready_read(const wchar_t* key,const char* role,DWORD* pid,uint64_t* start){
 wchar_t file[MAX_PATH];char expected[65],data[256];
 if(!path(key,file)||!nonce(expected)){return FALSE;}
 HANDLE h=CreateFileW(file,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_FLAG_OPEN_REPARSE_POINT,NULL);
 if(h==INVALID_HANDLE_VALUE){return FALSE;}
 BY_HANDLE_FILE_INFORMATION info;DWORD n=0;
 BOOL ok=GetFileType(h)==FILE_TYPE_DISK && GetFileInformationByHandle(h,&info) &&
 !(info.dwFileAttributes&(FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY)) &&
 info.nFileSizeHigh==0 && info.nFileSizeLow>0 && info.nFileSizeLow<sizeof(data) &&
 ReadFile(h,data,info.nFileSizeLow,&n,NULL) && n==info.nFileSizeLow;
 CloseHandle(h);if(!ok){return FALSE;}data[n]=0;
 char tag[24],token[65],kind[8],pid_text[16],start_text[24];int used=0;
 if(sscanf(data,"%23s %64s %7s %15s %23s%n",tag,token,kind,pid_text,start_text,&used)!=5 ||
 strcmp(tag,"UURB_FULL_READY_V1") || strcmp(token,expected)||strcmp(kind,role)||used<0||
 (DWORD)used+1!=n||data[used]!='\n'){return FALSE;}
 for(const char* a=pid_text;*a;a++){if(*a<'0'||*a>'9'){return FALSE;}}
 for(const char* a=start_text;*a;a++){if(*a<'0'||*a>'9'){return FALSE;}}
 errno=0;char* end;unsigned long long p=strtoull(pid_text,&end,10);
 if(errno==ERANGE||*end||!p||p>UINT32_MAX){return FALSE;}
 errno=0;unsigned long long s=strtoull(start_text,&end,10);
 if(errno==ERANGE||*end||!s){return FALSE;}*pid=(DWORD)p;*start=(uint64_t)s;return TRUE;
}
BOOL uurb_ready_publish(const wchar_t* key,const char* role){
 wchar_t file[MAX_PATH];char token[65],line[256];
 if(!path(key,file)||!nonce(token)){return FALSE;}
 DWORD pid=GetCurrentProcessId();uint64_t start=uurb_creation(GetCurrentProcess());if(!start){return FALSE;}
 int bytes=snprintf(line,sizeof(line),"UURB_FULL_READY_V1 %s %s %lu %llu\n",token,role,(unsigned long)pid,(unsigned long long)start);
 if(bytes<=0||(size_t)bytes>=sizeof(line)){return FALSE;}
 HANDLE h=CreateFileW(file,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
 if(h==INVALID_HANDLE_VALUE){
  DWORD old_pid;uint64_t old_start;
  return GetLastError()==ERROR_FILE_EXISTS && uurb_ready_read(key,role,&old_pid,&old_start) && old_pid==pid && old_start==start;
 }
 DWORD written=0;BOOL ok=WriteFile(h,line,(DWORD)bytes,&written,NULL)&&written==(DWORD)bytes&&FlushFileBuffers(h);
 CloseHandle(h);return ok;
}
