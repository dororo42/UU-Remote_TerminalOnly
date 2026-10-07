#ifndef UURB_RDP_BACKEND_H
#define UURB_RDP_BACKEND_H
#include <windows.h>
#include "uurb_rdp_state.h"
BOOL uurb_rdp_enabled(void);
#define UURB_RDP_QUERY_MAGIC 0x46515255UL
BOOL uurb_rdp_probe(DWORD*);
BOOL uurb_rdp_semantic_idle(DWORD*);
BOOL uurb_rdp_snapshot(uurb_rdp_state*,DWORD*);
BOOL uurb_rdp_validate(DWORD,const INPUT*,DWORD*);
DWORD uurb_rdp_send(DWORD,const INPUT*,DWORD*);
BOOL uurb_rdp_publish_identity(void);
void uurb_rdp_upstream_closed(void);
BOOL uurb_rdp_needs_recovery(void);
#endif
