#include "full-input.h"
#include <stdlib.h>
#include <string.h>
#include <limits.h>
struct ui_batch { uint64_t sequence,generation,deadline,transaction,owner_epoch;
 ui_geometry geometry; uint32_t count,cursor,source_count,text_units,deleted;
 uint32_t required,remote_deleted,expected_units,prior_units,semantic_first; uint64_t prefix_epoch;
 uint32_t erase_sources[UI_PRIMITIVES_MAX/2];
 size_t bytes; ui_primitive* primitives; uint16_t *text,*expected,*prior;
 enum ui_sem_phase semantic; ui_receipt receipt; bool prevalidated,commit_claimed,observation_claimed; uint64_t paste_started; uint32_t paste_keys;
 };
uint32_t ui_get32(const uint8_t* p) { return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
uint64_t ui_get64(const uint8_t* p) { return ui_get32(p)|((uint64_t)ui_get32(p+4)<<32); }
void ui_put32(uint8_t* p,uint32_t n) { for(unsigned i=0;i<4;i++){ p[i]=(uint8_t)(n>>(i*8)); } }
void ui_put64(uint8_t* p,uint64_t n) { ui_put32(p,(uint32_t)n);ui_put32(p+4,(uint32_t)(n>>32)); }
static uint64_t now(const ui_core* c) { return c->api.now(c->api.opaque); }
static bool geometry_equal(const ui_geometry* a,const ui_geometry* b) {
 return a->epoch==b->epoch && a->width==b->width && a->height==b->height &&
 a->dpi==b->dpi && a->scale_n==b->scale_n && a->scale_d==b->scale_d &&
 a->origin_x==b->origin_x && a->origin_y==b->origin_y && a->view_x==b->view_x &&
 a->view_y==b->view_y && a->view_w==b->view_w && a->view_h==b->view_h;
}
static bool valid_geometry(const ui_geometry* g) { return g->epoch && g->width &&
 g->width<=65536 && g->height && g->height<=65536 && g->view_w>1 && g->view_h>1 &&
 g->view_w<=65536 && g->view_h<=65536 && g->dpi>=48 && g->dpi<=768 &&
 g->scale_n && g->scale_n<=65536 && g->scale_d && g->scale_d<=65536; }
static void free_batch(ui_batch* b) { if(!b){ return; } if(b->text){memset(b->text,0,b->text_units*sizeof(uint16_t));free(b->text);}if(b->expected){memset(b->expected,0,b->expected_units*sizeof(uint16_t));free(b->expected);}
 if(b->prior){memset(b->prior,0,b->prior_units*sizeof(uint16_t));free(b->prior);}
 free(b->primitives);free(b); }
static void complete(ui_core* c,ui_batch* b,enum ui_status status) {
 b->receipt.status=status;b->receipt.unsent=b->count-b->cursor;
 b->receipt.semantic=b->semantic;b->receipt.edit_units=b->text_units;
 b->receipt.edit_deleted=b->deleted;b->receipt.longest_call_ns=c->longest_call_ns;
 b->receipt.remote_uncertain=c->remote_uncertain;
 size_t at=(c->completion_head+c->completion_count)%UI_QUEUE_BATCHES;
 /* Completed receipts themselves consume backpressure capacity until drained. */
 c->completed[at]=b->receipt;c->completion_count++;
 c->queue_bytes-=b->bytes;free_batch(b);
 for(size_t i=1;i<c->queued;i++){ c->queue[i-1]=c->queue[i]; }c->queued--;
}
void ui_init(ui_core* c,const ui_backend* api,uint64_t gen,const uint8_t secret[32],const ui_geometry* g,bool semantic) {
 memset(c,0,sizeof(*c));c->api=*api;c->phase=UI_OPEN;c->generation=gen;c->next_sequence=1;
 c->geometry=*g;c->semantic_enabled=semantic;c->semantic_epoch=1;memcpy(c->secret,secret,32);
}
static bool add(ui_batch* b,uint32_t op,uint32_t flags,int32_t a,int32_t d,uint32_t source) {
 if(b->count==UI_PRIMITIVES_MAX){ return false; }
 b->primitives[b->count++]=(ui_primitive){op,flags,a,d,source};return true;
}
static bool high(uint16_t u){return u>=0xd800 && u<=0xdbff;}
static bool low(uint16_t u){return u>=0xdc00 && u<=0xdfff;}
static bool utf16(const uint16_t* t,uint32_t n) {
 for(uint32_t i=0;i<n;i++){if(!t[i]){ return false; }if(high(t[i])){if(++i>=n||!low(t[i])){ return false; }}else if(low(t[i])){ return false; }}return true;
}
static bool pop_scalar(uint16_t* text,uint32_t* units){
 if(!*units){ return false; }
 uint16_t last=text[--*units];
 if(low(last)){if(!*units||!high(text[*units-1])){ return false; }(*units)--;}
 return true;
}
static bool edit(ui_batch* b,const ui_event* e,const uint16_t* text,uint32_t units,uint32_t source){
 if(e->flags || e->a<0 || e->b<0 || e->c<0 || e->d || e->aux ||
 (uint64_t)(uint32_t)e->b+(uint32_t)e->c>units || !utf16(text+(uint32_t)e->b,(uint32_t)e->c)){ return false; }
 for(int32_t i=0;i<e->a;i++){
  if(b->text_units){if(!pop_scalar(b->text,&b->text_units)){ return false; }}
  else{
   if(b->remote_deleted==UI_PRIMITIVES_MAX/2 || !pop_scalar(b->expected,&b->expected_units)){ return false; }
   b->erase_sources[b->remote_deleted++]=source;
  }
 }
 b->deleted+=(uint32_t)e->a;
 for(uint32_t i=0;i<(uint32_t)e->c;i++){
  uint16_t u=text[(uint32_t)e->b+i];
  if(u==13){u=10;if(i+1<(uint32_t)e->c && text[(uint32_t)e->b+i+1]==10){ i++; }}
  if(b->text_units==UI_TEXT_MAX){ return false; }b->text[b->text_units++]=u;
 }
 return true;
}
static bool point(const ui_geometry* g,int32_t x,int32_t y,int32_t* ox,int32_t* oy) {
 int64_t px=(int64_t)x-g->view_x,py=(int64_t)y-g->view_y;
 if(px<0||py<0||px>=g->view_w||py>=g->view_h){ return false; }
 *ox=(int32_t)((px*(g->width-1)+(g->view_w-1)/2)/(g->view_w-1));
 *oy=(int32_t)((py*(g->height-1)+(g->view_h-1)/2)/(g->view_h-1));return true;
}
static bool expand(ui_batch* b,const ui_event* e,const uint16_t* t,uint32_t units,uint32_t index) {
 int32_t x=0,y=0; if(e->reserved){ return false; }
 switch(e->op){
 case UI_ABSOLUTE:
  if(e->flags||e->c||e->d||e->aux||!point(&b->geometry,e->a,e->b,&x,&y)){ return false; }
  b->required|=UI_CAP_MOUSE;return add(b,UI_ABSOLUTE,0,x,y,index);
 case UI_BUTTON:
  if(e->flags>1||e->a<1||e->a>5||e->aux||!point(&b->geometry,e->b,e->c,&x,&y)||e->d){ return false; }
  b->required|=e->a<=3?UI_CAP_MOUSE:UI_CAP_EXT;
  /* Store button in low byte, down in bit8; event coordinates are mapped. */
  return add(b,UI_BUTTON,(uint32_t)e->a|(e->flags<<8),x,y,index);
 case UI_CURRENT_BUTTON:
  if(e->flags>1||e->a<1||e->a>5||e->b||e->c||e->d||e->aux){return false;}
  b->required|=UI_CAP_REL|(e->a<=3?UI_CAP_MOUSE:UI_CAP_EXT);
  return add(b,UI_RELATIVE,(uint32_t)e->a|(e->flags<<8),0,0,index);
 case UI_RELATIVE:{
  if(e->flags||e->c||e->d||e->aux){ return false; }b->required|=UI_CAP_REL;
  int64_t dx=e->a,dy=e->b;
  do {int32_t sx=dx>INT16_MAX?INT16_MAX:dx<INT16_MIN?INT16_MIN:(int32_t)dx;
      int32_t sy=dy>INT16_MAX?INT16_MAX:dy<INT16_MIN?INT16_MIN:(int32_t)dy;
      if(!add(b,UI_RELATIVE,0,sx,sy,index)){ return false; }dx-=sx;dy-=sy;}while(dx||dy);return true;}
 case UI_WHEEL:{
  if(e->flags>1||e->d||e->aux||!point(&b->geometry,e->b,e->c,&x,&y)){ return false; }
  b->required|=UI_CAP_MOUSE|(e->flags?UI_CAP_HWHEEL:0);
  int64_t delta=e->a;if(!delta){ return false; }
  while(delta){int32_t part=delta>255?255:delta< -255? -255:(int32_t)delta;
   uint32_t flags=(e->flags?0x0400u:0x0200u)|(part<0?0x0100u:0u)|((uint32_t)part&0x01ffu);
   if(!add(b,UI_WHEEL,flags,x,y,index)){ return false; }delta-=part;}return true;}
 case UI_KEY:
  if(e->flags&~15u || (e->flags&3u)==3u || e->a<1||e->a>255||e->b||e->c||e->d||e->aux||
    ((e->flags&8u)&&!(e->flags&4u))){ return false; }
  b->required|=UI_CAP_KEY;return add(b,UI_KEY,e->flags,e->a,0,index);
 case UI_UNICODE:
  if(e->flags>1||e->a<1||e->a>65535||high((uint16_t)e->a)||low((uint16_t)e->a)||e->b||e->c||e->d||e->aux){ return false; }
  b->required|=UI_CAP_UNICODE;return add(b,UI_UNICODE,e->flags,e->a,0,index);
 case UI_TEXT:
  if(e->flags||e->a<0||e->b<0||e->c||e->d||e->aux||
    (uint64_t)(uint32_t)e->a+(uint32_t)e->b>units||!utf16(t+(uint32_t)e->a,(uint32_t)e->b)){ return false; }
  b->required|=UI_CAP_UNICODE;
  for(uint32_t i=0;i<(uint32_t)e->b;i++){ if(!add(b,UI_UNICODE,1,t[(uint32_t)e->a+i],0,index)||
    !add(b,UI_UNICODE,0,t[(uint32_t)e->a+i],0,index)){ return false; } }return true;
 case UI_SYNC:case UI_FOCUS:
  if(e->flags||e->a<0||e->a>15||e->b||e->c||e->d||e->aux){ return false; }
  b->required|=e->op==UI_SYNC?UI_CAP_SYNC:UI_CAP_FOCUS;return add(b,e->op,0,e->a,0,index);
 case UI_PAUSE:
  if(e->flags||e->a||e->b||e->c||e->d||e->aux){ return false; }
  b->required|=UI_CAP_PAUSE;return add(b,UI_PAUSE,0,0,0,index);
 case UI_QOE:
  if(e->flags||e->b||e->c||e->d||e->aux){ return false; }
  b->required|=UI_CAP_QOE;return add(b,UI_QOE,0,e->a,0,index);
 case UI_EDIT:
  b->required|=UI_CAP_SEMANTIC;return edit(b,e,t,units,index);
 case UI_COMMIT:
  if(e->flags||e->a||e->b||e->c||e->d||e->aux||(!b->text_units&&!b->remote_deleted)){ return false; }
  b->required|=UI_CAP_SEMANTIC|UI_CAP_KEY;b->semantic=UI_VALIDATED_EDITS;
  return add(b,UI_COMMIT,0,0,0,index);
 default:return false;
 }
}
static enum ui_status live_for(ui_core* c,const ui_batch* b) {
 ui_live l;memset(&l,0,sizeof(l));
 if(!c->api.live(c->api.opaque,&l)||!l.associated||!l.active||l.suspended){ return UI_INACTIVE; }
 if(!geometry_equal(&b->geometry,&l.geometry)){ return UI_STALE; }
 if((l.caps&b->required)!=b->required){ return UI_UNSUPPORTED; }
 return UI_OK;
}
static bool held_modifiers(const ui_core* c) {
 static const unsigned codes[]={0x2a,0x36,0x12a,0x136,0x22a,0x236,0x38,0x138,0x238,0x5b,0x15b,0x25b,0x5c,0x15c,0x25c};
 for(size_t i=0;i<sizeof(codes)/sizeof(codes[0]);i++){ if(c->keys[codes[i]]){ return true; } }
 return c->keys[0x2f]!=0 || c->keys[0x21d]!=0 || c->keys[0x1d]==2 || c->keys[0x11d]==2; /* V held or E1 control */
}
static unsigned key_index(const ui_primitive* p){return (unsigned)p->a+((p->flags&3u)*256u);}
static void ledger(ui_core* c,const ui_primitive* p,bool before,bool accepted) {
 uint8_t* slot=NULL;bool down=false;
 if(p->op==UI_KEY){slot=&c->keys[key_index(p)];down=(p->flags&4)!=0;}
 if(p->op==UI_UNICODE){slot=&c->unicode[(uint16_t)p->a];down=p->flags!=0;}
 if(p->op==UI_BUTTON || (p->op==UI_RELATIVE && (p->flags&255u))){slot=&c->buttons[(p->flags&255u)-1u];down=(p->flags&256u)!=0;}
 if(!slot){ return; }
 if(before){if(down){ *slot=2; }}
 else if(accepted){*slot=down?1:0;}
 /* Failed releases retain their possible ownership; nothing is replayed ordinarily. */
}
enum ui_status ui_accept(ui_core* c,const uint8_t* w,size_t len,ui_receipt* rejection) {
 memset(rejection,0,sizeof(*rejection));rejection->status=UI_BAD_WIRE;
 if(len<UI_HEADER||len>UI_WIRE_MAX||ui_get32(w)!=UI_MAGIC||ui_get32(w+4)!=UI_VERSION||ui_get32(w+8)!=len||ui_get32(w+12)>2){ return UI_BAD_WIRE; }
 uint64_t gen=ui_get64(w+16),seq=ui_get64(w+24),expiry=ui_get64(w+32);
 rejection->generation=gen;rejection->sequence=seq;
 unsigned mismatch=0;for(unsigned i=0;i<32;i++){ mismatch|=(unsigned)(w[40+i]^c->secret[i]); }
 if(mismatch||gen!=c->generation){ return rejection->status=UI_AUTH; }
 if(c->phase!=UI_OPEN){ return rejection->status=UI_CLOSED; }
 if(seq!=c->next_sequence||seq==UINT64_MAX){ return rejection->status=UI_REPLAY; }
 if(expiry<=now(c)||expiry-now(c)>UINT64_C(5000000000)){ return rejection->status=UI_EXPIRED; }
 uint32_t operation=ui_get32(w+12);
 uint32_t count=ui_get32(w+128),units=ui_get32(w+132);
 if(operation==1){
  if(len!=UI_HEADER||count||units||ui_get32(w+124)||ui_get32(w+136)||ui_get32(w+140)){ return UI_BAD_WIRE; }
  c->next_sequence++;rejection->status=UI_OK;return UI_OK;
 }
 if((!count && !operation)||count>UI_EVENTS_MAX||units>UI_TEXT_MAX||ui_get32(w+136)||ui_get32(w+140)||
   UI_HEADER+(uint64_t)count*UI_EVENT_BYTES+(uint64_t)units*2u!=len){ return UI_BAD_WIRE; }
 ui_geometry g={ui_get64(w+72),ui_get32(w+80),ui_get32(w+84),ui_get32(w+88),ui_get32(w+92),ui_get32(w+96),
 (int32_t)ui_get32(w+100),(int32_t)ui_get32(w+104),(int32_t)ui_get32(w+108),(int32_t)ui_get32(w+112),ui_get32(w+116),ui_get32(w+120)};
 if(ui_get32(w+124)||!valid_geometry(&g)){ return rejection->status=UI_STALE; }
 if(operation==2){
  if(len!=UI_HEADER||count||units||c->queued||c->generation!=gen){ return rejection->status=UI_BUSY; }
  if(g.epoch!=c->geometry.epoch||g.width!=c->geometry.width||g.height!=c->geometry.height||g.epoch==UINT64_MAX){ return rejection->status=UI_STALE; }
  g.epoch++;c->geometry=g;c->next_sequence++;rejection->status=UI_OK;return UI_OK;
 }
 if(!geometry_equal(&g,&c->geometry)){ return rejection->status=UI_STALE; }
 if(c->queued+c->completion_count>=UI_QUEUE_BATCHES){ return rejection->status=UI_BUSY; }
 ui_batch* b=calloc(1,sizeof(*b));uint16_t* t=calloc(units?units:1,sizeof(*t));
 if(!b||!t){free(b);free(t);return rejection->status=UI_BUSY;}
 b->primitives=calloc(UI_PRIMITIVES_MAX,sizeof(*b->primitives));b->text=calloc(UI_TEXT_MAX,sizeof(*b->text));
 b->expected=calloc(UI_TEXT_MAX,sizeof(*b->expected));b->prior=calloc(UI_TEXT_MAX,sizeof(*b->prior));
 if(!b->primitives||!b->text||!b->expected||!b->prior){free_batch(b);free(t);return rejection->status=UI_BUSY;}
 b->prior_units=c->acknowledged_units;b->expected_units=c->acknowledged_units;b->prefix_epoch=c->semantic_epoch;
 memcpy(b->prior,c->acknowledged,c->acknowledged_units*sizeof(uint16_t));
 memcpy(b->expected,c->acknowledged,c->acknowledged_units*sizeof(uint16_t));
 b->sequence=seq;b->generation=gen;b->deadline=expiry;b->geometry=g;b->source_count=count;
 for(uint32_t i=0;i<units;i++){const uint8_t* p=w+UI_HEADER+count*UI_EVENT_BYTES+i*2;t[i]=(uint16_t)(p[0]|((uint16_t)p[1]<<8));}
 bool edits=false,committed=false;
 for(uint32_t i=0;i<count;i++){
 const uint8_t* p=w+UI_HEADER+i*UI_EVENT_BYTES;
 ui_event e={ui_get32(p),ui_get32(p+4),(int32_t)ui_get32(p+8),(int32_t)ui_get32(p+12),(int32_t)ui_get32(p+16),(int32_t)ui_get32(p+20),ui_get32(p+24),ui_get32(p+28)};
 if(committed||(edits && e.op!=UI_EDIT && e.op!=UI_COMMIT)||!expand(b,&e,t,units,i)){ goto bad; }
 if(e.op==UI_EDIT){if(!edits){ b->semantic_first=i; }edits=true;}if(e.op==UI_COMMIT){committed=true;if(i+1!=count){ goto bad; }}
 }
 memset(t,0,units*sizeof(*t));free(t);t=NULL;
 if(edits&&!committed){ goto bad; }
 if(committed){
  if(b->expected_units+b->text_units>UI_TEXT_MAX || b->count+2u*b->remote_deleted+3u>UI_PRIMITIVES_MAX){ goto bad; }
  memcpy(b->expected+b->expected_units,b->text,b->text_units*sizeof(uint16_t));b->expected_units+=b->text_units;
  for(size_t i=0;i<c->queued;i++){ if(c->queue[i]->required&UI_CAP_SEMANTIC){free_batch(b);return rejection->status=UI_BUSY;} }
  if(c->transaction_counter==UINT64_MAX||c->semantic_epoch==UINT64_MAX){free_batch(b);return rejection->status=UI_CLOSED;}
 }
 if((b->required&UI_CAP_SEMANTIC)&&(!c->semantic_enabled||!c->api.owner_current)){free_batch(b);return rejection->status=UI_UNSUPPORTED;}
 b->bytes=sizeof(*b)+UI_PRIMITIVES_MAX*sizeof(*b->primitives)+UI_TEXT_MAX*(sizeof(*b->text)+sizeof(*b->expected)+sizeof(*b->prior));
 if(b->bytes>UI_QUEUE_BYTES-c->queue_bytes){free_batch(b);return rejection->status=UI_BUSY;}
 b->transaction=committed?++c->transaction_counter:0;
 b->receipt=(ui_receipt){0};b->receipt.sequence=seq;b->receipt.generation=gen;b->receipt.source_total=count;
 b->receipt.total=b->count;b->receipt.transaction=b->transaction;b->receipt.ambiguous=UINT32_MAX;b->receipt.first_ambiguous=UINT32_MAX;b->receipt.observed_first=UINT32_MAX;b->receipt.observed_last=UINT32_MAX;
 c->queue[c->queued++]=b;c->queue_bytes+=b->bytes;c->next_sequence++;
 rejection->status=UI_OK;return UI_OK;
 bad: if(t){memset(t,0,units*sizeof(*t));free(t);}free_batch(b);return UI_BAD_WIRE;
}
static bool send_one(ui_core* c,ui_batch* b,const ui_primitive* p) {
 b->receipt.source_index=p->source;b->receipt.tier=UI_WRAPPER_ATTEMPTED;
 b->receipt.attempted++;ledger(c,p,true,false);
 if(p->op==UI_ABSOLUTE||p->op==UI_BUTTON||p->op==UI_RELATIVE){c->pointer_known=false;}
 uint64_t start=now(c);bool accepted=c->api.send(c->api.opaque,p);uint64_t end=now(c);
 if(end>=start && end-start>c->longest_call_ns){ c->longest_call_ns=end-start; }
 ledger(c,p,false,accepted);b->cursor++;
 if(accepted && (p->op==UI_ABSOLUTE||p->op==UI_BUTTON)){c->pointer_x=p->a;c->pointer_y=p->b;c->pointer_known=true;}
 if(!accepted){b->receipt.ambiguous=b->cursor-1;
 if(b->receipt.first_ambiguous==UINT32_MAX){ b->receipt.first_ambiguous=b->cursor-1; }
 c->remote_uncertain=true;return false;}
 /* Compound wrappers may partially send even on TRUE: exact inner prefix unknown. */
 if(p->op==UI_PAUSE||p->op==UI_FOCUS){b->receipt.ambiguous=b->cursor-1;b->receipt.compound_unknown++;
 if(b->receipt.first_ambiguous==UINT32_MAX){ b->receipt.first_ambiguous=b->cursor-1; }
 c->remote_uncertain=true;}
 return true;
}
static bool release_one(ui_core* c,uint64_t deadline) {
 ui_live l;memset(&l,0,sizeof(l));
 if(!c->api.live(c->api.opaque,&l)||!l.associated||!l.active||l.suspended){c->remote_uncertain=true;c->release_cursor=66565;return false;}
 while(c->release_cursor<66565 && now(c)<deadline){unsigned n=c->release_cursor++;ui_primitive p={0};uint8_t* slot;
 if(n<1024){slot=&c->keys[n];p=(ui_primitive){UI_KEY,n/256u,(int32_t)(n%256u),0,0};}
 else if(n<66560){slot=&c->unicode[n-1024];p=(ui_primitive){UI_UNICODE,0,(int32_t)(n-1024),0,0};}
 else{slot=&c->buttons[n-66560];
  /* GRD mouse button events warp even without MOVE. After relative/ambiguous
   * motion, zero-delta relative release preserves the actual receiver position.
   * This internal flags path is never accepted from the relative wire opcode. */
  p=c->pointer_known?(ui_primitive){UI_BUTTON,n-66560+1u,c->pointer_x,c->pointer_y,0}:
   (ui_primitive){UI_RELATIVE,n-66560+1u,0,0,0};}
 if(!*slot){ continue; }
 uint32_t required=p.op==UI_KEY?UI_CAP_KEY:p.op==UI_UNICODE?UI_CAP_UNICODE:p.op==UI_RELATIVE?(UI_CAP_REL|(p.flags<=3?UI_CAP_MOUSE:UI_CAP_EXT)):(p.flags<=3?UI_CAP_MOUSE:UI_CAP_EXT);
 if((l.caps&required)!=required){c->remote_uncertain=true;return true;}
 if(c->api.send(c->api.opaque,&p)){ *slot=0; }else c->remote_uncertain=true;return true;}
 return false;
}
static void abort_batch(ui_core* c,ui_batch* b,enum ui_status status,bool context_live) {
 bool touched=b->receipt.attempted!=0;
 complete(c,b,status);
 if(touched){ ui_close(c,context_live); }
}
void ui_slice(ui_core* c) {
 if(c->phase==UI_DEAD||c->phase==UI_QUIESCED){ return; }
 uint64_t start=now(c);unsigned dispatched=0;
 uint64_t deadline=start>UINT64_MAX-UI_SLICE_NS?UINT64_MAX:start+UI_SLICE_NS;
 if(c->phase==UI_CLOSING){while(dispatched<UI_SLICE_CALLS&&now(c)<deadline){if(!release_one(c,deadline)){ break; }dispatched++;}return;}
 while(c->queued&&dispatched<UI_SLICE_CALLS&&now(c)-start<UI_SLICE_NS){
 ui_batch* b=c->queue[0];
 if(b->generation!=c->generation||now(c)>=b->deadline){abort_batch(c,b,UI_EXPIRED,true);if(c->phase!=UI_OPEN){ return; }continue;}
 enum ui_status valid=live_for(c,b);if(valid!=UI_OK){abort_batch(c,b,valid,valid!=UI_INACTIVE);if(c->phase!=UI_OPEN){ return; }continue;}
 if((b->required&UI_CAP_SEMANTIC)&&b->prefix_epoch!=c->semantic_epoch){complete(c,b,UI_STALE);continue;}
 if(!b->prevalidated){b->prevalidated=true;b->receipt.tier=UI_PREVALIDATED;}
 if(b->cursor==b->count){
  if(b->semantic==UI_ORDERED_RDP_CHORD){b->receipt.status=UI_WAIT_RECIPIENT;return;}
  complete(c,b,UI_OK);continue;}
 ui_primitive* p=&b->primitives[b->cursor];
 if(p->op==UI_COMMIT){
  if(b->semantic==UI_VALIDATED_EDITS){if(held_modifiers(c)||(b->remote_deleted&&(c->keys[0x1d]||c->keys[0x11d]))){complete(c,b,UI_CONFLICT);continue;}b->receipt.status=UI_WAIT_OWNER;return;}
  if(b->semantic==UI_COMMITTING){ return; }
  if(b->semantic!=UI_CLIPBOARD_OWNER_COMMITTED){complete(c,b,UI_OWNER_LOST);continue;}
  if(held_modifiers(c)||(b->remote_deleted&&(c->keys[0x1d]||c->keys[0x11d]))||!c->api.owner_current(c->api.opaque,c->generation,b->transaction,b->owner_epoch)){
   b->semantic=UI_SEM_FAILED;complete(c,b,UI_OWNER_LOST);continue;}
  b->semantic=UI_GENERATION_PASTE_BARRIER;b->paste_started=now(c);b->receipt.owner_committed=true;b->receipt.owner_epoch=b->owner_epoch;
  /* Preserve caller Ctrl held: choose already-held side without changing it. */
  bool own_ctrl=!c->keys[0x1d]&&!c->keys[0x11d];
  uint32_t src=p->source;
  unsigned chord_count=b->text_units?(own_ctrl?4u:2u):0u;
  b->paste_keys=2u*b->remote_deleted+chord_count;
  unsigned at=0;
  for(uint32_t i=0;i<b->remote_deleted;i++){
   p[at++]=(ui_primitive){UI_KEY,4,0x0e,0,b->erase_sources[i]};
   p[at++]=(ui_primitive){UI_KEY,0,0x0e,0,b->erase_sources[i]};
  }
  if(b->text_units){
   if(own_ctrl){ p[at++]=(ui_primitive){UI_KEY,4,0x1d,0,src}; }
   p[at++]=(ui_primitive){UI_KEY,4,0x2f,0,src};p[at++]=(ui_primitive){UI_KEY,0,0x2f,0,src};
   if(own_ctrl){ p[at++]=(ui_primitive){UI_KEY,0,0x1d,0,src}; }
  }
  /* Expansion capacity was checked before accepting/committing this transaction. */
  b->count=b->cursor+at;b->receipt.total=b->count;b->semantic=UI_ORDERED_RDP_CHORD;
  continue;
 }
 /* Selection can change while a long replacement spans slices. Never paste
  * a replaced owner's data; already-attempted edits remain in the receipt. */
 if(b->semantic==UI_ORDERED_RDP_CHORD &&
 !c->api.owner_current(c->api.opaque,c->generation,b->transaction,b->owner_epoch)){
  abort_batch(c,b,UI_OWNER_LOST,true);if(c->phase!=UI_OPEN){ return; }continue;
 }
 /* Recheck immediately before every primitive, including slices after waits. */
 if(now(c)>=b->deadline){abort_batch(c,b,UI_EXPIRED,true);if(c->phase!=UI_OPEN){ return; }continue;}
 enum ui_status fresh=live_for(c,b);
 if(fresh!=UI_OK){abort_batch(c,b,fresh,fresh!=UI_INACTIVE);if(c->phase!=UI_OPEN){ return; }continue;}
 if(!send_one(c,b,p)){complete(c,b,UI_AMBIGUOUS);ui_close(c,true);return;}dispatched++;
 }
}
bool ui_take_receipt(ui_core* c,ui_receipt* r){if(!c->completion_count){ return false; }*r=c->completed[c->completion_head];c->completion_head=(c->completion_head+1)%UI_QUEUE_BATCHES;c->completion_count--;return true;}
bool ui_take_commit_job(ui_core* c,ui_commit_job* j){
 if(c->phase!=UI_OPEN||!c->queued){ return false; }ui_batch* b=c->queue[0];
 if(!b->prevalidated||b->cursor>=b->count||b->primitives[b->cursor].op!=UI_COMMIT||b->semantic!=UI_VALIDATED_EDITS||b->commit_claimed||held_modifiers(c)||(b->remote_deleted&&(c->keys[0x1d]||c->keys[0x11d]))){ return false; }
 memset(j,0,sizeof(*j));j->generation=b->generation;j->sequence=b->sequence;j->transaction=b->transaction;j->deadline_ns=b->deadline;j->geometry=b->geometry;
 j->units=b->text_units;j->deleted=b->deleted;j->prior_units=b->prior_units;j->expected_units=b->expected_units;
 memcpy(j->prior,b->prior,b->prior_units*sizeof(uint16_t));memcpy(j->expected,b->expected,b->expected_units*sizeof(uint16_t));memcpy(j->text,b->text,b->text_units*sizeof(*j->text));b->commit_claimed=true;b->semantic=UI_COMMITTING;return true;
}
enum ui_status ui_commit_finished(ui_core* c,const ui_commit_job* j,bool success,uint64_t owner_epoch){
 if(c->phase!=UI_OPEN||!c->queued||j->generation!=c->generation||c->queue[0]->transaction!=j->transaction||c->queue[0]->sequence!=j->sequence){
  for(size_t i=0;i<c->completion_count;i++){
   ui_receipt* r=&c->completed[(c->completion_head+i)%UI_QUEUE_BATCHES];
   if(r->generation==j->generation && r->sequence==j->sequence && r->transaction==j->transaction){
    r->owner_committed=success && owner_epoch!=0;r->owner_epoch=owner_epoch;
    if(r->owner_committed){ r->semantic=UI_CLIPBOARD_OWNER_COMMITTED; }
   }
  }
  return UI_CLOSED;
 }
 ui_batch* b=c->queue[0];
 if(b->transaction!=j->transaction||b->sequence!=j->sequence||b->semantic!=UI_COMMITTING){ return UI_REPLAY; }
 if(!success||!owner_epoch){b->semantic=UI_SEM_FAILED;complete(c,b,UI_OWNER_LOST);return UI_OWNER_LOST;}
 b->owner_epoch=owner_epoch;b->receipt.owner_committed=true;b->receipt.owner_epoch=owner_epoch;
 if(now(c)>=b->deadline){b->semantic=UI_SEM_FAILED;complete(c,b,UI_EXPIRED);return UI_EXPIRED;}
 b->semantic=UI_CLIPBOARD_OWNER_COMMITTED;return UI_OK;
}
bool ui_take_observation_job(ui_core* c,ui_commit_job* j,uint64_t* owner_epoch){
 if(c->phase!=UI_OPEN||!c->queued){ return false; }
 ui_batch* b=c->queue[0];
 if(b->semantic!=UI_ORDERED_RDP_CHORD||b->cursor!=b->count||b->observation_claimed){ return false; }
 memset(j,0,sizeof(*j));j->generation=b->generation;j->sequence=b->sequence;
 j->transaction=b->transaction;j->deadline_ns=b->deadline;j->geometry=b->geometry;j->units=b->text_units;j->deleted=b->deleted;j->prior_units=b->prior_units;j->expected_units=b->expected_units;
 memcpy(j->prior,b->prior,b->prior_units*sizeof(uint16_t));memcpy(j->expected,b->expected,b->expected_units*sizeof(uint16_t));
 memcpy(j->text,b->text,b->text_units*sizeof(*j->text));*owner_epoch=b->owner_epoch;
 b->observation_claimed=true;return true;
}
enum ui_status ui_recipient_ack(ui_core* c,const ui_recipient_evidence* witness){
 if(!witness||c->phase!=UI_OPEN||witness->generation!=c->generation||!c->queued){ return UI_CLOSED; }
 ui_batch* b=c->queue[0];
 if(b->transaction!=witness->transaction||b->semantic!=UI_ORDERED_RDP_CHORD||b->cursor!=b->count){ return UI_REPLAY; }
 bool verified=witness->owner_epoch==b->owner_epoch && witness->whole_buffer &&
 witness->units==b->expected_units && witness->key_events>=b->paste_keys && witness->paste_events==(b->text_units?1u:0u) &&
 witness->focus_continuous && witness->interval_start>=b->paste_started &&
 witness->interval_end>=witness->interval_start && witness->interval_end<=b->deadline &&
 witness->interval_end<=now(c);
 if(verified){ verified=memcmp(witness->whole_buffer,b->expected,b->expected_units*sizeof(uint16_t))==0; }
 if(!verified){b->semantic=UI_SEM_FAILED;complete(c,b,UI_AMBIGUOUS);return UI_AMBIGUOUS;}
 memcpy(c->acknowledged,b->expected,b->expected_units*sizeof(uint16_t));c->acknowledged_units=b->expected_units;c->semantic_epoch++;
 b->receipt.observed_first=b->semantic_first;b->receipt.observed_last=b->source_count-1;
 b->semantic=UI_RECIPIENT_ACK;b->receipt.tier=UI_RECIPIENT_OBSERVED;complete(c,b,UI_OK);return UI_OK;
}
void ui_close(ui_core* c,bool context_live){
 if(c->phase==UI_CLOSING && !context_live){c->remote_uncertain=true;c->release_cursor=66565;return;}
 if(c->phase!=UI_OPEN){ return; }c->phase=UI_CLOSING;
 while(c->queued){ui_batch* b=c->queue[0];complete(c,b,context_live?UI_CLOSED:UI_UNREACHABLE);}
 if(!context_live){c->remote_uncertain=true;c->release_cursor=66565;}
}
bool ui_locally_drained(const ui_core* c){return c->phase==UI_CLOSING && !c->queued && c->release_cursor>=66565;}
void ui_mark_quiesced(ui_core* c,bool timer_quiet,bool worker_quiet){if(c->phase==UI_CLOSING&&timer_quiet&&worker_quiet&&ui_locally_drained(c)){ c->phase=UI_QUIESCED; }}
void ui_destroy(ui_core* c){if(c->phase!=UI_QUIESCED){ return; }memset(c->secret,0,32);memset(c->keys,0,sizeof(c->keys));memset(c->unicode,0,sizeof(c->unicode));memset(c->acknowledged,0,sizeof(c->acknowledged));c->phase=UI_DEAD;}
void ui_encode_receipt(uint8_t out[UI_RECEIPT_BYTES],const ui_receipt* r){
 memset(out,0,UI_RECEIPT_BYTES);ui_put32(out,UI_MAGIC);ui_put32(out+4,UI_VERSION);ui_put32(out+8,UI_RECEIPT_BYTES);
 ui_put32(out+12,r->status);ui_put64(out+16,r->generation);ui_put64(out+24,r->sequence);ui_put32(out+32,r->tier);
 ui_put32(out+36,r->source_index);ui_put32(out+40,r->source_total);ui_put32(out+44,r->attempted);ui_put32(out+48,r->total);
 ui_put32(out+52,r->ambiguous);ui_put32(out+56,r->submitted);ui_put32(out+60,r->unsent);ui_put32(out+64,r->semantic);
 ui_put32(out+68,r->edit_units);ui_put32(out+72,r->edit_deleted);ui_put32(out+76,r->owner_committed?1:0);
 ui_put32(out+80,r->remote_uncertain?1:0);ui_put64(out+88,r->transaction);ui_put64(out+96,r->owner_epoch);ui_put64(out+104,r->longest_call_ns);ui_put32(out+112,r->compound_unknown);ui_put32(out+116,r->first_ambiguous);ui_put32(out+120,r->observed_first);ui_put32(out+124,r->observed_last);
}
