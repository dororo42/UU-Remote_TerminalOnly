#ifndef UURB_RDP_STATE_H
#define UURB_RDP_STATE_H
#include <stdint.h>
#define UURB_RDP_STATE_MAGIC UINT32_C(0x53525255)
#define UURB_RDP_STATE_VERSION 1u
/* Authenticated local wrapper state, NEVER recipient/delivery state. */
typedef struct {
 uint32_t version,position_known; uint64_t generation,epoch;
 int32_t x,y; uint8_t keys[256],known[256];
} uurb_rdp_state;
_Static_assert(sizeof(uurb_rdp_state)==544, "same-process-family state ABI");
#endif
