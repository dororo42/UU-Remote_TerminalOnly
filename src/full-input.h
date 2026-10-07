#ifndef UURB_FULL_INPUT_H
#define UURB_FULL_INPUT_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
/* Wire fields are individually little-endian; never cast untrusted bytes. */
#define UI_MAGIC UINT32_C(0x55494632)
#define UI_VERSION 3u
#define UI_HEADER 144u
#define UI_EVENT_BYTES 32u
#define UI_WIRE_MAX 65536u
#define UI_EVENTS_MAX 256u
#define UI_PRIMITIVES_MAX 4096u
#define UI_QUEUE_BYTES 1048576u
#define UI_QUEUE_BATCHES 16u
#define UI_SLICE_CALLS 32u
#define UI_SLICE_NS UINT64_C(2000000)
#define UI_TEXT_MAX 16384u
#define UI_RECEIPT_BYTES 128u
#define UI_CAP_KEY UINT32_C(1)
#define UI_CAP_UNICODE UINT32_C(2)
#define UI_CAP_MOUSE UINT32_C(4)
#define UI_CAP_EXT UINT32_C(8)
#define UI_CAP_REL UINT32_C(16)
#define UI_CAP_HWHEEL UINT32_C(32)
#define UI_CAP_SYNC UINT32_C(64)
#define UI_CAP_PAUSE UINT32_C(128)
#define UI_CAP_FOCUS UINT32_C(256)
#define UI_CAP_QOE UINT32_C(512)
#define UI_CAP_SEMANTIC UINT32_C(1024)
enum ui_status { UI_OK, UI_BAD_WIRE, UI_AUTH, UI_REPLAY, UI_EXPIRED, UI_BUSY,
 UI_STALE, UI_UNSUPPORTED, UI_INACTIVE, UI_AMBIGUOUS, UI_CLOSED, UI_UNREACHABLE,
 UI_CONFLICT, UI_OWNER_LOST, UI_WAIT_OWNER, UI_WAIT_RECIPIENT, UI_LOCAL_PENDING };
enum ui_phase { UI_OPEN, UI_CLOSING, UI_QUIESCED, UI_DEAD };
enum ui_tier { UI_NONE, UI_PREVALIDATED, UI_WRAPPER_ATTEMPTED,
 UI_TRANSPORT_SUBMITTED, UI_RECIPIENT_OBSERVED };
enum ui_op { UI_ABSOLUTE=1, UI_BUTTON, UI_RELATIVE, UI_WHEEL, UI_KEY,
 UI_UNICODE, UI_TEXT, UI_SYNC, UI_PAUSE, UI_FOCUS, UI_QOE,
 UI_EDIT, UI_COMMIT, UI_CURRENT_BUTTON };
enum ui_sem_phase { UI_SEM_NONE, UI_VALIDATED_EDITS, UI_COMMITTING,
 UI_CLIPBOARD_OWNER_COMMITTED, UI_GENERATION_PASTE_BARRIER,
 UI_ORDERED_RDP_CHORD, UI_RECIPIENT_ACK, UI_SEM_FAILED };
/* Button values 1 left, 2 right, 3 middle, 4/5 extended. KEY flags: E0=1,
 * E1=2, down=4, repeat=8. WHEEL flags: horizontal=1. EDIT a deletes Unicode
 * scalars from this uncommitted edit buffer; b/c are UTF16 offset/count. */
typedef struct { uint32_t op, flags; int32_t a,b,c,d; uint32_t aux,reserved; } ui_event;
typedef struct { uint64_t epoch; uint32_t width,height,dpi,scale_n,scale_d;
 int32_t origin_x,origin_y,view_x,view_y; uint32_t view_w,view_h; } ui_geometry;
typedef struct { uint32_t op,flags; int32_t a,b; uint32_t source; } ui_primitive;
typedef struct { uint64_t sequence,generation; enum ui_status status;
 enum ui_tier tier; uint32_t source_index,source_total,attempted,total,ambiguous;
 uint32_t submitted,unsent,compound_unknown,first_ambiguous,observed_first,observed_last; enum ui_sem_phase semantic;
 uint32_t edit_units,edit_deleted; bool owner_committed,remote_uncertain;
 uint64_t transaction,owner_epoch,longest_call_ns; } ui_receipt;
typedef struct { uint64_t generation,sequence,transaction,deadline_ns;
 ui_geometry geometry; uint32_t units,deleted,prior_units,expected_units; uint16_t text[UI_TEXT_MAX],prior[UI_TEXT_MAX],expected[UI_TEXT_MAX]; } ui_commit_job;
typedef struct { uint64_t generation,transaction,owner_epoch,interval_start,interval_end;
 uint32_t units,key_events,paste_events; bool focus_continuous;
 const uint16_t* whole_buffer; } ui_recipient_evidence;
typedef struct { bool active,suspended,associated; uint32_t caps;
 ui_geometry geometry; } ui_live;
typedef struct { void* opaque;
 uint64_t (*now)(void*);
 /* live and owner_current must be bounded, nonblocking, no pipe/clipboard I/O. */
 bool (*live)(void*,ui_live*);
 bool (*send)(void*,const ui_primitive*);
 /* send result is wrapper acceptance ONLY, not submission/delivery evidence. */
 bool (*owner_current)(void*,uint64_t generation,uint64_t transaction,uint64_t epoch);
 } ui_backend;
typedef struct ui_batch ui_batch;
typedef struct { ui_backend api; enum ui_phase phase; uint64_t generation,next_sequence;
 uint8_t secret[32]; ui_geometry geometry;
 ui_batch* queue[UI_QUEUE_BATCHES]; size_t queued,queue_bytes;
 uint8_t keys[1024],unicode[65536],buttons[5];
 uint32_t release_cursor; int32_t pointer_x,pointer_y; bool remote_uncertain,semantic_enabled,pointer_known;
 uint64_t longest_call_ns,transaction_counter,semantic_epoch;
 uint32_t acknowledged_units; uint16_t acknowledged[UI_TEXT_MAX];
 ui_receipt completed[UI_QUEUE_BATCHES]; size_t completion_head,completion_count;
 } ui_core;
void ui_init(ui_core*,const ui_backend*,uint64_t,const uint8_t[32],const ui_geometry*,bool);
/* Caller serializes core access. Decode runs on worker, not the timer. */
enum ui_status ui_accept(ui_core*,const uint8_t*,size_t,ui_receipt*);
void ui_slice(ui_core*);
bool ui_take_receipt(ui_core*,ui_receipt*);
bool ui_take_commit_job(ui_core*,ui_commit_job*);
/* Provider runs commit outside core lock/main loop. Completion cannot resurrect
 * a canceled generation. Epoch is an actual owner witness, not a clipboard BOOL. */
enum ui_status ui_commit_finished(ui_core*,const ui_commit_job*,bool,uint64_t);
bool ui_take_observation_job(ui_core*,ui_commit_job*,uint64_t* owner_epoch);
enum ui_status ui_recipient_ack(ui_core*,const ui_recipient_evidence*);
void ui_close(ui_core*,bool context_live);
bool ui_locally_drained(const ui_core*);
void ui_mark_quiesced(ui_core*,bool timer_quiet,bool worker_quiet);
void ui_destroy(ui_core*);
void ui_encode_receipt(uint8_t[UI_RECEIPT_BYTES],const ui_receipt*);
uint32_t ui_get32(const uint8_t*);
uint64_t ui_get64(const uint8_t*);
void ui_put32(uint8_t*,uint32_t);
void ui_put64(uint8_t*,uint64_t);
#endif
