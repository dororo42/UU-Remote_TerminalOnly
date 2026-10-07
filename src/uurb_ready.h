#ifndef UURB_READY_H
#define UURB_READY_H
#include <windows.h>
#include <stdint.h>
uint64_t uurb_creation(HANDLE);
BOOL uurb_ready_publish(const wchar_t*,const char*);
BOOL uurb_ready_read(const wchar_t*,const char*,DWORD*,uint64_t*);
#endif
