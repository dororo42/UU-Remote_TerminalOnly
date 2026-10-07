#ifndef UU_X11_MANAGER_CAPTURE_API_H
#define UU_X11_MANAGER_CAPTURE_API_H

/* Public Xlib/Render ABI types only. The runtime loads existing libraries;
 * building this manager sidecar does not require X development packages. */
typedef struct _XDisplay MCDisplay;
typedef struct _XImage MCImage;
typedef unsigned long MCWindow;
typedef unsigned long MCDrawable;
typedef unsigned long MCPixmap;
typedef unsigned long MCAtom;
typedef unsigned long MCPicture;

typedef struct {
    void *ext_data;
    unsigned long visualid;
    int visual_class;
    unsigned long red_mask, green_mask, blue_mask;
    int bits_per_rgb, map_entries;
} MCVisual;

typedef struct {
    int x, y, width, height, border_width, depth;
    MCVisual *visual;
    MCWindow root;
    int window_class, bit_gravity, win_gravity, backing_store;
    unsigned long backing_planes, backing_pixel;
    int save_under;
    unsigned long colormap;
    int map_installed, map_state;
    long all_event_masks, your_event_mask, do_not_propagate_mask;
    int override_redirect;
    void *screen;
} MCWindowAttributes;

typedef struct { char *res_name, *res_class; } MCClassHint;
typedef struct {
    int type;
    MCDisplay *display;
    unsigned long resourceid, serial;
    unsigned char error_code, request_code, minor_code;
} MCErrorEvent;
typedef int (*MCErrorHandler)(MCDisplay *, MCErrorEvent *);

typedef struct {
    short red, red_mask, green, green_mask, blue, blue_mask, alpha, alpha_mask;
} MCRenderDirect;
typedef struct {
    unsigned long id;
    int type, depth;
    MCRenderDirect direct;
    unsigned long colormap;
} MCRenderFormat;
typedef struct { unsigned short red, green, blue, alpha; } MCRenderColor;
typedef union {
    long padding[24];
    struct {
        int type;
        unsigned long serial;
        int send_event;
        MCDisplay *display;
        MCWindow window;
        MCAtom message_type;
        int format;
        union { char bytes[20]; short shorts[10]; long longs[5]; } data;
    } message;
} MCEvent;

#endif
