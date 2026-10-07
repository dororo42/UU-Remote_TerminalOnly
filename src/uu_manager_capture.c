#define _GNU_SOURCE
#include "x11_manager_capture_api.h"
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

#define MAX_WINDOWS 64U
#define MAX_PRESSES 128U
#define MAX_TREE_NODES 4096U
#define MAX_PIXELS (16U * 1024U * 1024U)
#define MANAGER_REFRESH_NS UINT64_C(66666667)
#define VIEWABLE 2
#define INPUT_OUTPUT 1
#define XA_CARDINAL 6UL
#define PICT_OP_SRC 1
#define PICT_OP_OVER 3

struct x_api {
    int (*init_threads)(void);
    MCDisplay *(*open_display)(const char *);
    int (*close_display)(MCDisplay *);
    char *(*display_string)(MCDisplay *);
    MCWindow (*default_root)(MCDisplay *);
    int (*attributes)(MCDisplay *, MCWindow, MCWindowAttributes *);
    int (*query_tree)(MCDisplay *, MCWindow, MCWindow *, MCWindow *, MCWindow **, unsigned int *);
    int (*translate)(MCDisplay *, MCWindow, MCWindow, int, int, int *, int *, MCWindow *);
    int (*class_hint)(MCDisplay *, MCWindow, MCClassHint *);
    int (*fetch_name)(MCDisplay *, MCWindow, char **);
    MCAtom (*intern_atom)(MCDisplay *, const char *, int);
    int (*property)(MCDisplay *, MCWindow, MCAtom, long, long, int, MCAtom,
                    MCAtom *, int *, unsigned long *, unsigned long *, unsigned char **);
    int (*free_data)(void *);
    MCPixmap (*create_pixmap)(MCDisplay *, MCDrawable, unsigned int, unsigned int, unsigned int);
    int (*free_pixmap)(MCDisplay *, MCPixmap);
    MCErrorHandler (*set_error_handler)(MCErrorHandler);
    unsigned long (*next_request)(MCDisplay *);
    int (*sync)(MCDisplay *, int);
    int (*raise_window)(MCDisplay *, MCWindow);
    int (*set_input_focus)(MCDisplay *, MCWindow, int, unsigned long);
    int (*get_input_focus)(MCDisplay *, MCWindow *, int *);
    int (*query_pointer)(MCDisplay *, MCWindow, MCWindow *, MCWindow *, int *, int *, int *, int *, unsigned int *);
    int (*send_event)(MCDisplay *, MCWindow, int, long, MCEvent *);
    int (*composite_version)(MCDisplay *, int *, int *);
    MCPixmap (*name_pixmap)(MCDisplay *, MCWindow);
    void (*redirect_window)(MCDisplay *, MCWindow, int);
    void (*unredirect_window)(MCDisplay *, MCWindow, int);
    MCRenderFormat *(*visual_format)(MCDisplay *, MCVisual *);
    MCPicture (*create_picture)(MCDisplay *, MCDrawable, const MCRenderFormat *, unsigned long, const void *);
    void (*free_picture)(MCDisplay *, MCPicture);
    void (*fill_rectangle)(MCDisplay *, int, MCPicture, const MCRenderColor *, int, int, unsigned int, unsigned int);
    void (*composite)(MCDisplay *, int, MCPicture, MCPicture, MCPicture,
                      int, int, int, int, int, int, unsigned int, unsigned int);
    MCImage *(*get_image)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int);
    MCImage *(*get_sub_image)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int, MCImage *, int, int);
    int (*shm_get_image)(MCDisplay *, MCDrawable, MCImage *, int, int, unsigned long);
    int (*fake_key)(MCDisplay *, unsigned int, int, unsigned long);
    int (*fake_button)(MCDisplay *, unsigned int, int, unsigned long);
    int (*fake_motion)(MCDisplay *, int, int, int, unsigned long);
    int (*fake_device_key)(MCDisplay *, void *, unsigned int, int, int *, int, unsigned long);
    int (*fake_device_button)(MCDisplay *, void *, unsigned int, int, int *, int, unsigned long);
    int (*fake_device_motion)(MCDisplay *, void *, int, int, int *, int, unsigned long);
    void *(*open_device)(MCDisplay *, unsigned long);
    int (*close_device)(MCDisplay *, void *);
};

struct gui_window {
    MCWindow client, frame;
    int x, y, width, height;
    int source_x, source_y;
    int modal, client_depth, override_redirect;
    MCWindowAttributes frame_attributes;
};

static struct x_api api;
static pthread_once_t once = PTHREAD_ONCE_INIT;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static MCDisplay *private_display;
static MCWindow root, main_window;
static unsigned long main_pid;
static MCPixmap canvas;
static MCPicture canvas_picture;
static int canvas_width, canvas_height, canvas_depth;
static int old_x, old_y, old_width, old_height;
static MCAtom pid_atom, type_atom, dialog_atom, transient_atom;
static MCAtom name_atom, utf8_atom;
static struct gui_window windows[MAX_WINDOWS];
static unsigned int window_count, tree_nodes;
static struct gui_window candidates[MAX_WINDOWS];
static unsigned int candidate_count;
static MCWindow redirected_frames[MAX_WINDOWS];
static unsigned int redirected_count;
static int controller_role;
static int isolated_manager;
static uint64_t refresh_ns = MANAGER_REFRESH_NS;
static int target_x, target_y, target_width, target_height;
static char target_name[1024];
static uint64_t last_refresh;
static uint64_t last_geometry;
static char expected_display[256];
static int configured, available;
static int pointer_x, pointer_y, pointer_known;
static MCWindow last_input_window;
struct accepted_press { unsigned long device; unsigned int code; int kind; };
static struct accepted_press presses[MAX_PRESSES];
static unsigned int press_count;
static int last_status = -1;
static int tree_overflow;
static int ready_written;
static FILE *log_file;
static unsigned int input_trace_remaining;
static MCErrorHandler previous_error_handler;
static MCWindow scan_candidate, scan_protected[MAX_WINDOWS * 2];
static unsigned int scan_protected_count;
static unsigned long scan_candidate_serial;
static unsigned long error_first, error_last;
static int private_error;

static void log_message(const char *format, ...)
{
    va_list arguments;
    FILE *output = log_file != NULL ? log_file : stderr;
    va_start(arguments, format);
    fputs("UU manager capture: ", output);
    vfprintf(output, format, arguments);
    fputc('\n', output);
    fflush(output);
    va_end(arguments);
}

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return 0;
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}

static int load_symbol(void *library, const char *name, void *destination, size_t size)
{
    void *symbol = dlsym(library, name);
    if (symbol == NULL || size != sizeof(symbol))
        return 0;
    memcpy(destination, &symbol, size);
    return 1;
}

#define LOAD(library, field, symbol) \
    do { if (!load_symbol(library, symbol, &api.field, sizeof(api.field))) return; } while (0)

static void initialize(void)
{
    void *xlib = dlopen("libX11.so.6", RTLD_NOW | RTLD_LOCAL);
    void *xext;
    void *composite_library;
    void *render_library;
    void *test_library;
    void *input_library;
    const char *display_name = getenv("UURB_MANAGER_EXPECTED_DISPLAY");
    const char *window_id = getenv("UURB_MANAGER_WINDOW_ID");
    const char *log_path = getenv("UURB_MANAGER_CAPTURE_LOG");
    const char *role = getenv("UURB_MANAGER_CAPTURE_ROLE");
    const char *fps_text = getenv("UURB_MANAGER_CAPTURE_FPS");
    const char *isolate_text = getenv("UURB_MANAGER_CAPTURE_ISOLATE_ROOT");
    const char *input_trace = getenv("UURB_MANAGER_INPUT_TRACE");
    char *end;

    if (display_name != NULL && *display_name != '\0' &&
        strlen(display_name) < sizeof(expected_display)) {
        strcpy(expected_display, display_name);
        configured = 1;
    }
    if (log_path != NULL && *log_path != '\0') {
        log_file = fopen(log_path, "a");
        if (log_file != NULL)
            setvbuf(log_file, NULL, _IOLBF, 0);
    }
    if (xlib == NULL)
        return;
    LOAD(xlib, init_threads, "XInitThreads");
    if (configured && !api.init_threads())
        return;
    LOAD(xlib, get_image, "XGetImage");
    LOAD(xlib, get_sub_image, "XGetSubImage");
    LOAD(xlib, display_string, "XDisplayString");
    LOAD(xlib, default_root, "XDefaultRootWindow");
    xext = dlopen("libXext.so.6", RTLD_NOW | RTLD_LOCAL);
    test_library = dlopen("libXtst.so.6", RTLD_NOW | RTLD_LOCAL);
    if (xext == NULL || test_library == NULL)
        return;
    LOAD(xext, shm_get_image, "XShmGetImage");
    LOAD(test_library, fake_key, "XTestFakeKeyEvent");
    LOAD(test_library, fake_button, "XTestFakeButtonEvent");
    LOAD(test_library, fake_motion, "XTestFakeMotionEvent");
    LOAD(test_library, fake_device_key, "XTestFakeDeviceKeyEvent");
    LOAD(test_library, fake_device_button, "XTestFakeDeviceButtonEvent");
    LOAD(test_library, fake_device_motion, "XTestFakeDeviceMotionEvent");
    if (!configured)
        return;
    if (role != NULL && strcmp(role, "manager") != 0 && strcmp(role, "controller") != 0)
        return;
    controller_role = role != NULL && strcmp(role, "controller") == 0;
    if (isolate_text != NULL && strcmp(isolate_text, "0") != 0 && strcmp(isolate_text, "1") != 0)
        return;
    isolated_manager = !controller_role && isolate_text != NULL && strcmp(isolate_text, "1") == 0;
    if (fps_text != NULL) {
        unsigned long fps;
        errno = 0;
        fps = strtoul(fps_text, &end, 10);
        if (errno != 0 || *fps_text == '\0' || *end != '\0' || fps == 0 || fps > 60)
            return;
        refresh_ns = (UINT64_C(1000000000) + fps - 1) / fps;
    } else if (controller_role)
        refresh_ns = UINT64_C(16666667);
    input_library = dlopen("libXi.so.6", RTLD_NOW | RTLD_LOCAL);
    if (input_library == NULL)
        return;
    LOAD(input_library, open_device, "XOpenDevice");
    LOAD(input_library, close_device, "XCloseDevice");
    if (window_id == NULL || *window_id == '\0')
        return;
    errno = 0;
    main_window = strtoul(window_id, &end, 0);
    if (errno != 0 || *end != '\0' || main_window == 0)
        return;
    LOAD(xlib, open_display, "XOpenDisplay");
    LOAD(xlib, close_display, "XCloseDisplay");
    LOAD(xlib, attributes, "XGetWindowAttributes");
    LOAD(xlib, send_event, "XSendEvent");
    LOAD(xlib, query_tree, "XQueryTree");
    LOAD(xlib, translate, "XTranslateCoordinates");
    LOAD(xlib, class_hint, "XGetClassHint");
    LOAD(xlib, fetch_name, "XFetchName");
    LOAD(xlib, intern_atom, "XInternAtom");
    LOAD(xlib, property, "XGetWindowProperty");
    LOAD(xlib, free_data, "XFree");
    LOAD(xlib, create_pixmap, "XCreatePixmap");
    LOAD(xlib, free_pixmap, "XFreePixmap");
    LOAD(xlib, set_error_handler, "XSetErrorHandler");
    LOAD(xlib, next_request, "XNextRequest");
    LOAD(xlib, sync, "XSync");
    LOAD(xlib, raise_window, "XRaiseWindow");
    LOAD(xlib, set_input_focus, "XSetInputFocus");
    LOAD(xlib, query_pointer, "XQueryPointer");
    if (input_trace != NULL && strcmp(input_trace, "1") == 0 && log_file != NULL &&
        load_symbol(xlib, "XGetInputFocus", &api.get_input_focus, sizeof(api.get_input_focus)))
        input_trace_remaining = 32;
    composite_library = dlopen("libXcomposite.so.1", RTLD_NOW | RTLD_LOCAL);
    render_library = dlopen("libXrender.so.1", RTLD_NOW | RTLD_LOCAL);
    if (composite_library == NULL || render_library == NULL)
        return;
    LOAD(composite_library, composite_version, "XCompositeQueryVersion");
    LOAD(composite_library, name_pixmap, "XCompositeNameWindowPixmap");
    LOAD(composite_library, redirect_window, "XCompositeRedirectWindow");
    LOAD(composite_library, unredirect_window, "XCompositeUnredirectWindow");
    LOAD(render_library, visual_format, "XRenderFindVisualFormat");
    LOAD(render_library, create_picture, "XRenderCreatePicture");
    LOAD(render_library, free_picture, "XRenderFreePicture");
    LOAD(render_library, fill_rectangle, "XRenderFillRectangle");
    LOAD(render_library, composite, "XRenderComposite");
    available = 1;
}

static int trap_private_error(MCDisplay *display, MCErrorEvent *event)
{
    if (display == private_display && event->serial >= error_first &&
        event->serial <= error_last) {
        /* A tree snapshot may outlive an unrelated candidate. Contain only
         * that candidate's synchronous first attributes request; owned
         * identities, later probes and Composite failures remain fatal. */
        if (scan_candidate != 0 && event->resourceid == scan_candidate &&
            event->serial == scan_candidate_serial && event->error_code == 3 &&
            event->request_code == 3 && event->minor_code == 0)
            return 0;
        private_error = event->error_code;
        return 0;
    }
    if (previous_error_handler != NULL)
        return previous_error_handler(display, event);
    return 0;
}

static void begin_requests(void)
{
    private_error = 0;
    error_first = api.next_request(private_display);
    error_last = ULONG_MAX;
    previous_error_handler = api.set_error_handler(trap_private_error);
}

static int end_requests(void)
{
    error_last = api.next_request(private_display) - 1;
    api.sync(private_display, 0);
    api.set_error_handler(previous_error_handler);
    return private_error == 0;
}

static unsigned long property_scalar(MCWindow window, MCAtom atom, MCAtom requested)
{
    unsigned char *data = NULL;
    MCAtom actual = 0;
    unsigned long count = 0, remaining = 0, value = 0;
    int format = 0;
    if (api.property(private_display, window, atom, 0, 1, 0, requested,
                     &actual, &format, &count, &remaining, &data) == 0 &&
        actual == requested && format == 32 && count == 1 && data != NULL)
        value = *(unsigned long *)data;
    if (data != NULL)
        api.free_data(data);
    return value;
}

static int window_name(MCWindow window, char **name)
{
    MCAtom actual = 0;
    unsigned char *data = NULL;
    unsigned long count = 0, remaining = 0;
    int format = 0, status;
    *name = NULL;
    status = api.property(private_display, window, name_atom, 0, 256, 0, utf8_atom,
                          &actual, &format, &count, &remaining, &data);
    if (status == 0 &&
        actual == utf8_atom && format == 8 && count < sizeof(target_name) &&
        remaining == 0 && data != NULL && memchr(data, '\0', count) == NULL) {
        for (unsigned long index = 0; index < count;) {
            unsigned char first = data[index++];
            unsigned int needed;
            uint32_t value, minimum;
            if (first < 0x80)
                continue;
            if (first >= 0xc2 && first <= 0xdf) {
                needed = 1; value = first & 0x1f; minimum = 0x80;
            } else if (first >= 0xe0 && first <= 0xef) {
                needed = 2; value = first & 0x0f; minimum = 0x800;
            } else if (first >= 0xf0 && first <= 0xf4) {
                needed = 3; value = first & 0x07; minimum = 0x10000;
            } else
                goto invalid;
            if (needed > count - index)
                goto invalid;
            for (unsigned int part = 0; part < needed; ++part) {
                unsigned char next = data[index++];
                if ((next & 0xc0) != 0x80)
                    goto invalid;
                value = (value << 6) | (next & 0x3f);
            }
            if (value < minimum || value > 0x10ffff ||
                (value >= 0xd800 && value <= 0xdfff))
                goto invalid;
        }
        *name = (char *)data;
        return 1;
    }
invalid:
    if (data != NULL)
        api.free_data(data);
    if (status != 0 || actual != 0)
        return -1;
    /* Only absence of the UTF8 property permits a legacy STRING fallback.
     * An unreadable COMPOUND_TEXT title is not an unnamed popup. */
    data = NULL;
    status = api.property(private_display, window, 39UL, 0, 256, 0, 31UL,
                          &actual, &format, &count, &remaining, &data);
    int invalid_legacy = status != 0 || (actual != 0 &&
        (actual != 31UL || format != 8 || count >= sizeof(target_name) || remaining != 0 ||
         data == NULL || memchr(data, '\0', count) != NULL));
    if (data != NULL)
        api.free_data(data);
    if (invalid_legacy)
        return -1;
    return api.fetch_name(private_display, window, name);
}

static int matching_class(MCWindow window)
{
    MCClassHint hint = {NULL, NULL};
    int result = 0;
    if (api.class_hint(private_display, window, &hint))
        result = (hint.res_class != NULL && strcasecmp(hint.res_class, "gameviewer.exe") == 0) ||
                 (hint.res_name != NULL && strcasecmp(hint.res_name, "gameviewer.exe") == 0);
    if (hint.res_name != NULL)
        api.free_data(hint.res_name);
    if (hint.res_class != NULL)
        api.free_data(hint.res_class);
    return result;
}

static int same_owner(MCWindow window)
{
    return matching_class(window) &&
           property_scalar(window, pid_atom, XA_CARDINAL) == main_pid;
}

static int is_modal(MCWindow window)
{
    MCAtom actual = 0;
    unsigned char *data = NULL;
    unsigned long count = 0, remaining = 0;
    int format = 0, modal = 0;
    if (api.property(private_display, window, type_atom, 0, 16, 0, 4UL,
                     &actual, &format, &count, &remaining, &data) == 0 &&
        actual == 4UL && format == 32 && data != NULL) {
        for (unsigned long index = 0; index < count; ++index)
            if (((unsigned long *)data)[index] == dialog_atom)
                modal = 1;
    }
    if (data != NULL)
        api.free_data(data);
    return modal || property_scalar(window, transient_atom, 33UL) == main_window;
}

static int generic_popup_related(MCWindow client, const MCWindowAttributes *attributes)
{
    char *name = NULL;
    MCWindow child;
    MCWindow sole_owner = 0;
    unsigned int matches = 0;
    int x, y, generic;
    if (!controller_role || attributes->depth != 32 || attributes->width > 640 ||
        attributes->height > 640 || attributes->override_redirect ||
        property_scalar(client, transient_atom, 33UL) != 0 ||
        property_scalar(client, type_atom, 4UL) != api.intern_atom(private_display, "_NET_WM_WINDOW_TYPE_NORMAL", 0))
        return 0;
    if (window_name(client, &name) < 0)
        return 0;
    generic = name != NULL && strcmp(name, "GameViewer") == 0;
    if (name != NULL)
        api.free_data(name);
    if (!generic || !api.translate(private_display, client, root, 0, 0, &x, &y, &child))
        return 0;
    for (unsigned int index = 0; index < candidate_count; ++index) {
        const struct gui_window *entry = &candidates[index];
        int controller;
        if (entry->width < 640 || entry->height < 360 || entry->override_redirect ||
            property_scalar(entry->client, transient_atom, 33UL) != 0 ||
            property_scalar(entry->client, type_atom, 4UL) != api.intern_atom(private_display, "_NET_WM_WINDOW_TYPE_NORMAL", 0) ||
            x < entry->x || y < entry->y || x + attributes->width > entry->x + entry->width ||
            y + attributes->height > entry->y + entry->height)
            continue;
        name = NULL;
        window_name(entry->client, &name);
        controller = name != NULL && *name != '\0' && strcmp(name, "网易UU远程") != 0 &&
                     strcmp(name, "UU Remote") != 0 && strcmp(name, "GameViewer") != 0;
        if (name != NULL)
            api.free_data(name);
        if (controller) {
            sole_owner = entry->client;
            ++matches;
        }
    }
    return matches == 1 && sole_owner == main_window;
}

static int related_window(MCWindow client, const MCWindowAttributes *attributes)
{
    MCWindow ancestor;
    MCWindow child;
    char *name = NULL;
    int x, y, same_name;
    if (client == main_window)
        return 1;
    ancestor = property_scalar(client, transient_atom, 33UL);
    if (ancestor != 0) {
        for (unsigned int depth = 0; depth < 8; ++depth) {
            MCWindowAttributes parent_attributes;
            if (ancestor == main_window)
                return 1;
            if (!same_owner(ancestor))
                return 0;
            /* Wine omits the transient hint on its NORMAL control center,
             * but nested dialogs still name that uniquely owned popup. */
            if (api.attributes(private_display, ancestor, &parent_attributes) &&
                generic_popup_related(ancestor, &parent_attributes))
                return 1;
            ancestor = property_scalar(ancestor, transient_atom, 33UL);
            if (ancestor == 0)
                return 0;
        }
        return 0;
    }
    /* Wine/Qt also uses unnamed or same-title NORMAL popup windows without a
     * transient hint. Keep that bounded fallback, but never merge another
     * independently titled controller session or the manager lobby. */
    if (generic_popup_related(client, attributes))
        return 1;
    if (window_name(client, &name) < 0)
        return 0;
    same_name = (name == NULL || *name == '\0' || strcmp(name, target_name) == 0) &&
                (!controller_role || name == NULL || strcmp(name, "GameViewer") != 0);
    if (name != NULL)
        api.free_data(name);
    return same_name && attributes->width <= target_width && attributes->height <= target_height &&
           api.translate(private_display, client, root, 0, 0, &x, &y, &child) &&
           x >= target_x && y >= target_y && x + attributes->width <= target_x + target_width &&
           y + attributes->height <= target_y + target_height;
}

static unsigned int controller_layer(MCWindow client)
{
    MCWindow ancestor;
    if (client == main_window)
        return 0;
    ancestor = property_scalar(client, transient_atom, 33UL);
    for (unsigned int level = 1; level <= 8 && ancestor != 0; ++level) {
        MCWindowAttributes attributes;
        if (ancestor == main_window)
            return level;
        if (api.attributes(private_display, ancestor, &attributes) &&
            generic_popup_related(ancestor, &attributes))
            return level + 1;
        ancestor = property_scalar(ancestor, transient_atom, 33UL);
    }
    return property_scalar(client, transient_atom, 33UL) == 0 ? 1 : MAX_WINDOWS;
}

static int append_window(MCWindow client, MCWindow frame, const MCWindowAttributes *attributes)
{
    struct gui_window *entry;
    MCWindow ignored;
    if (window_count >= MAX_WINDOWS) {
        tree_overflow = 1;
        return 0;
    }
    if (attributes->width <= 1 || attributes->height <= 1 ||
        (unsigned int)attributes->width > MAX_PIXELS / (unsigned int)attributes->height)
        return 0;
    entry = &windows[window_count];
    memset(entry, 0, sizeof(*entry));
    entry->client = client;
    entry->frame = frame;
    entry->width = attributes->width;
    entry->height = attributes->height;
    entry->client_depth = attributes->depth;
    entry->override_redirect = attributes->override_redirect;
    if (!api.attributes(private_display, frame, &entry->frame_attributes) ||
        !api.translate(private_display, client, root, 0, 0, &entry->x, &entry->y, &ignored) ||
        !api.translate(private_display, client, frame, 0, 0,
                       &entry->source_x, &entry->source_y, &ignored))
        return 0;
    entry->source_x += entry->frame_attributes.border_width;
    entry->source_y += entry->frame_attributes.border_width;
    entry->modal = client != main_window && is_modal(client);
    ++window_count;
    return 1;
}

static int scan_frame(MCWindow window, MCWindow frame, unsigned int depth)
{
    MCWindowAttributes attributes;
    MCWindow found_root, parent, *children = NULL;
    unsigned int count = 0;
    if (++tree_nodes > MAX_TREE_NODES || depth > 4) {
        tree_overflow = 1;
        return 0;
    }
    int protected = window == main_window || window == root;
    for (unsigned int index = 0; index < scan_protected_count; ++index)
        if (scan_protected[index] == window)
            protected = 1;
    for (unsigned int index = 0; index < window_count; ++index)
        if (windows[index].client == window || windows[index].frame == window)
            protected = 1;
    scan_candidate = protected ? 0 : window;
    scan_candidate_serial = api.next_request(private_display);
    int readable = api.attributes(private_display, window, &attributes);
    scan_candidate = 0;
    if (!readable)
        return 0;
    if (attributes.map_state != VIEWABLE)
        return 0;
    if (attributes.window_class == INPUT_OUTPUT && same_owner(window))
        return append_window(window, frame, &attributes);
    if (!api.query_tree(private_display, window, &found_root, &parent, &children, &count))
        return 0;
    if (count > MAX_TREE_NODES) {
        tree_overflow = 1;
        if (children != NULL)
            api.free_data(children);
        return 0;
    }
    for (unsigned int index = 0; index < count; ++index)
        (void)scan_frame(children[index], frame, depth + 1);
    if (children != NULL)
        api.free_data(children);
    return 1;
}

static int collect_windows(struct gui_window *main)
{
    MCWindowAttributes attributes;
    MCWindow tree_root, parent, *children = NULL;
    unsigned int count = 0, main_index = MAX_WINDOWS;
    int result = 0;
    char *name = NULL;
    if (window_count != 0) {
        scan_protected_count = 0;
        for (unsigned int index = 0; index < window_count; ++index) {
            scan_protected[scan_protected_count++] = windows[index].client;
            scan_protected[scan_protected_count++] = windows[index].frame;
        }
    }
    window_count = tree_nodes = 0;
    tree_overflow = 0;
    if (!api.attributes(private_display, main_window, &attributes) ||
        attributes.map_state != VIEWABLE || !same_owner(main_window))
        return 0;
    if (window_name(main_window, &name) < 0)
        return 0;
    snprintf(target_name, sizeof(target_name), "%s", name != NULL ? name : "");
    if (name != NULL)
        api.free_data(name);
    if (!api.translate(private_display, main_window, root, 0, 0, &target_x, &target_y, &parent))
        return 0;
    target_width = attributes.width;
    target_height = attributes.height;
    if (!api.query_tree(private_display, root, &tree_root, &parent, &children, &count) ||
        count > MAX_TREE_NODES)
        goto done;
    for (unsigned int index = 0; index < count; ++index)
        (void)scan_frame(children[index], children[index], 0);
    if (tree_overflow)
        goto done;
    candidate_count = window_count;
    memcpy(candidates, windows, candidate_count * sizeof(*candidates));
    window_count = 0;
    for (unsigned int index = 0; index < candidate_count; ++index) {
        MCWindowAttributes client_attributes;
        memset(&client_attributes, 0, sizeof(client_attributes));
        client_attributes.width = candidates[index].width;
        client_attributes.height = candidates[index].height;
        client_attributes.depth = candidates[index].client_depth;
        client_attributes.override_redirect = candidates[index].override_redirect;
        if (related_window(candidates[index].client, &client_attributes))
            windows[window_count++] = candidates[index];
    }
    for (unsigned int index = 0; index < window_count; ++index)
        if (windows[index].client == main_window)
            main_index = index;
    if (main_index == MAX_WINDOWS)
        goto done;
    *main = windows[main_index];
    if (controller_role) {
        unsigned int layers[MAX_WINDOWS];
        for (unsigned int index = 0; index < window_count; ++index) {
            layers[index] = controller_layer(windows[index].client);
            if (layers[index] == MAX_WINDOWS)
                goto done;
        }
        /* Openbox cannot infer that a NORMAL Qt control center belongs to its
         * controller. Keep verified parents below their transient children;
         * preserve the real stacking order between siblings at each level.
         * Both composition and native hit testing use this same ordering. */
        for (unsigned int index = 1; index < window_count; ++index) {
            struct gui_window entry = windows[index];
            unsigned int layer = layers[index], position = index;
            while (position > 0 && layers[position - 1] > layer) {
                windows[position] = windows[position - 1];
                layers[position] = layers[position - 1];
                --position;
            }
            windows[position] = entry;
            layers[position] = layer;
        }
    }
    last_geometry = monotonic_ns();
    result = 1;
done:
    if (children != NULL)
        api.free_data(children);
    if (!result)
        window_count = 0;
    return result;
}

static void write_ready(void)
{
    const char *path = getenv("UURB_MANAGER_CAPTURE_READY_FILE");
    const char *nonce = getenv("UURB_MANAGER_CAPTURE_NONCE");
    char record[512];
    int descriptor, length;
    size_t offset = 0;
    if (ready_written)
        return;
    if (path == NULL || *path == '\0' || nonce == NULL || strlen(nonce) != 36)
        goto failed;
    for (size_t index = 0; index < 36; ++index) {
        if (index == 8 || index == 13 || index == 18 || index == 23) {
            if (nonce[index] != '-')
                goto failed;
        } else if (!((nonce[index] >= '0' && nonce[index] <= '9') ||
                     (nonce[index] >= 'a' && nonce[index] <= 'f')))
            goto failed;
    }
    length = snprintf(record, sizeof(record), "%s %ld %lu %s\n", nonce,
                      (long)getpid(), main_window, expected_display);
    if (length <= 0 || (size_t)length >= sizeof(record))
        goto failed;
    descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0)
        goto failed;
    while (offset < (size_t)length) {
        ssize_t written = write(descriptor, record + offset, (size_t)length - offset);
        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0) {
            close(descriptor);
            goto failed;
        }
        offset += (size_t)written;
    }
    if (close(descriptor) != 0)
        goto failed;
    ready_written = 1;
    return;
failed:
    log_message("readiness handshake failed; ending management sidecar");
    _exit(70);
}

static void clear_rectangle(int x, int y, int width, int height)
{
    static const MCRenderColor black = {0, 0, 0, 65535};
    if (canvas_picture != 0 && width > 0 && height > 0)
        api.fill_rectangle(private_display, PICT_OP_SRC, canvas_picture, &black,
                           x, y, (unsigned int)width, (unsigned int)height);
}

static int ensure_canvas(const MCWindowAttributes *attributes)
{
    MCRenderFormat *format;
    if (attributes->width <= 0 || attributes->height <= 0 ||
        attributes->width > 8192 || attributes->height > 8192 ||
        (unsigned int)attributes->width > MAX_PIXELS / (unsigned int)attributes->height)
        return 0;
    if (canvas != 0 && canvas_width == attributes->width &&
        canvas_height == attributes->height && canvas_depth == attributes->depth)
        return 1;
    if (canvas_picture != 0)
        api.free_picture(private_display, canvas_picture);
    if (canvas != 0)
        api.free_pixmap(private_display, canvas);
    canvas = 0;
    canvas_picture = 0;
    format = api.visual_format(private_display, attributes->visual);
    if (format == NULL || format->type != 1 || format->depth != attributes->depth)
        return 0;
    canvas = api.create_pixmap(private_display, root, (unsigned int)attributes->width,
                               (unsigned int)attributes->height, (unsigned int)attributes->depth);
    if (canvas == 0)
        return 0;
    canvas_picture = api.create_picture(private_display, canvas, format, 0, NULL);
    canvas_width = attributes->width;
    canvas_height = attributes->height;
    canvas_depth = attributes->depth;
    clear_rectangle(0, 0, canvas_width, canvas_height);
    old_width = old_height = 0;
    return canvas_picture != 0;
}

static int draw_window(const struct gui_window *entry, const struct gui_window *main)
{
    int left = entry->x > main->x ? entry->x : main->x;
    int top = entry->y > main->y ? entry->y : main->y;
    int right = entry->x + entry->width < main->x + main->width ?
                entry->x + entry->width : main->x + main->width;
    int bottom = entry->y + entry->height < main->y + main->height ?
                 entry->y + entry->height : main->y + main->height;
    MCPixmap pixmap;
    MCPicture picture;
    MCRenderFormat *format;
    if (right <= left || bottom <= top)
        return 1;
    format = api.visual_format(private_display, entry->frame_attributes.visual);
    if (format == NULL || format->type != 1 ||
        format->depth != entry->frame_attributes.depth)
        return 0;
    if (controller_role || isolated_manager) {
        unsigned int index;
        for (index = 0; index < redirected_count; ++index)
            if (redirected_frames[index] == entry->frame)
                break;
        if (index == redirected_count) {
            if (redirected_count == MAX_WINDOWS)
                return 0;
            api.redirect_window(private_display, entry->frame, isolated_manager ? 1 : 0);
            redirected_frames[redirected_count++] = entry->frame;
        }
    }
    pixmap = api.name_pixmap(private_display, entry->frame);
    if (pixmap == 0)
        return 0;
    picture = api.create_picture(private_display, pixmap, format, 0, NULL);
    if (picture != 0) {
        api.composite(private_display, PICT_OP_OVER, picture, 0, canvas_picture,
                      entry->source_x + left - entry->x, entry->source_y + top - entry->y,
                      0, 0, left, top, (unsigned int)(right - left), (unsigned int)(bottom - top));
        api.free_picture(private_display, picture);
    }
    api.free_pixmap(private_display, pixmap);
    return picture != 0;
}

static void retire_redirected_frames(void)
{
    if ((!controller_role && !isolated_manager) || redirected_count == 0)
        return;
    begin_requests();
    for (unsigned int index = 0; index < redirected_count;) {
        unsigned int candidate;
        for (candidate = 0; candidate < window_count; ++candidate)
            if (windows[candidate].frame == redirected_frames[index])
                break;
        if (candidate < window_count) {
            ++index;
            continue;
        }
        api.unredirect_window(private_display, redirected_frames[index], isolated_manager ? 1 : 0);
        redirected_frames[index] = redirected_frames[--redirected_count];
    }
    /* A destroyed retired window can produce BadWindow. It is not a source
     * for the next frame; contain that cleanup error to our own connection. */
    (void)end_requests();
}

static void fit_related_popups(const struct gui_window *main)
{
    static unsigned int unsupported_warnings;
    static struct { MCWindow window; int x, y, width, height, desired_x, desired_y;
                    uint64_t last; unsigned int attempts; } pending[MAX_WINDOWS];
    uint64_t now = monotonic_ns();
    for (unsigned int index = 0; index < window_count; ++index) {
        const struct gui_window *entry = &windows[index];
        int x = entry->x, y = entry->y;
        MCEvent event;
        if (entry->client == main_window)
            continue;
        if (entry->width > main->width || entry->height > main->height) {
            if (unsupported_warnings++ < 8)
                log_message("owned popup exceeds controller viewport; cannot fit without resizing it");
            continue;
        }
        if (x < main->x) x = main->x;
        if (y < main->y) y = main->y;
        if (x + entry->width > main->x + main->width) x = main->x + main->width - entry->width;
        if (y + entry->height > main->y + main->height) y = main->y + main->height - entry->height;
        if ((x == entry->x && y == entry->y) || entry->frame == entry->client)
            continue;
        unsigned int slot;
        for (slot = 0; slot < MAX_WINDOWS; ++slot)
            if (pending[slot].window == entry->client || pending[slot].window == 0)
                break;
        if (slot == MAX_WINDOWS)
            continue;
        if (pending[slot].window != entry->client || pending[slot].x != entry->x ||
            pending[slot].y != entry->y || pending[slot].width != entry->width ||
            pending[slot].height != entry->height || pending[slot].desired_x != x || pending[slot].desired_y != y) {
            pending[slot].attempts = 0;
            pending[slot].last = 0;
        }
        if (pending[slot].attempts >= 3 ||
            (pending[slot].last && now - pending[slot].last < UINT64_C(1000000000)))
            continue;
        pending[slot].window = entry->client;
        pending[slot].x = entry->x; pending[slot].y = entry->y;
        pending[slot].width = entry->width; pending[slot].height = entry->height;
        pending[slot].desired_x = x; pending[slot].desired_y = y;
        pending[slot].last = now; pending[slot].attempts++;
        /* Only already-associated owned popups move. StaticGravity keeps
         * these coordinates at the client origin; never request focus. */
        memset(&event, 0, sizeof(event));
        event.message.type = 33;
        event.message.display = private_display;
        event.message.window = entry->client;
        event.message.message_type = api.intern_atom(private_display, "_NET_MOVERESIZE_WINDOW", 0);
        event.message.format = 32;
        event.message.data.longs[0] = 10 | (1L << 8) | (1L << 9) | (2L << 12);
        event.message.data.longs[1] = x;
        event.message.data.longs[2] = y;
        (void)api.send_event(private_display, root, 0, (1L << 19) | (1L << 20), &event);
    }
    for (unsigned int slot = 0; slot < MAX_WINDOWS; ++slot) {
        unsigned int index;
        for (index = 0; index < window_count; ++index)
            if (windows[index].client == pending[slot].window &&
                (windows[index].x < main->x || windows[index].y < main->y ||
                 windows[index].x + windows[index].width > main->x + main->width ||
                 windows[index].y + windows[index].height > main->y + main->height))
                break;
        if (index == window_count)
            memset(&pending[slot], 0, sizeof(pending[slot]));
    }
}

static int refresh_canvas(void)
{
    MCWindowAttributes root_attributes;
    struct gui_window main;
    int success = 0;

    begin_requests();
    if (!api.attributes(private_display, root, &root_attributes))
        goto done;
    if (!collect_windows(&main))
        goto done;
    if (controller_role) {
        /* A mode change and its ordinary WM fit are separate events. Keep
         * the verified target readable during that bounded transition. */
        if (main.x >= 0 && main.x <= 8192 && main.width <= 8192 - main.x &&
            main.x + main.width > root_attributes.width)
            root_attributes.width = main.x + main.width;
        if (main.y >= 0 && main.y <= 8192 && main.height <= 8192 - main.y &&
            main.y + main.height > root_attributes.height)
            root_attributes.height = main.y + main.height;
    }
    if (!ensure_canvas(&root_attributes))
        goto done;
    clear_rectangle(old_x, old_y, old_width, old_height);
    if (!end_requests())
        goto failed;
    retire_redirected_frames();
    begin_requests();
    if (controller_role)
        fit_related_popups(&main);
    old_x = main.x;
    old_y = main.y;
    old_width = main.width;
    old_height = main.height;
    clear_rectangle(main.x, main.y, main.width, main.height);
    /* Composite only owned top-levels, in actual root stacking order. Relay
     * pixels are never a source, even when it obscures the entire application. */
    for (unsigned int index = 0; index < window_count; ++index)
        if (!draw_window(&windows[index], &main))
            goto done;
    success = 1;
done:
    if (!end_requests())
        success = 0;
failed:
    if (!success && canvas_picture != 0) {
        begin_requests();
        clear_rectangle(old_x, old_y, old_width, old_height);
        (void)end_requests();
        window_count = 0;
    }
    if (last_status != success) {
        log_message(success ? "active; isolated Composite canvas" :
                              "safe blank; owned window or Composite backing unavailable");
        last_status = success;
    }
    last_refresh = monotonic_ns();
    if (success)
        write_ready();
    return canvas != 0 && canvas_picture != 0;
}

static int prepare_private_display(void)
{
    int major = 0, minor = 0;
    if (private_display != NULL)
        return 1;
    private_display = api.open_display(expected_display);
    if (private_display == NULL)
        return 0;
    root = api.default_root(private_display);
    pid_atom = api.intern_atom(private_display, "_NET_WM_PID", 0);
    type_atom = api.intern_atom(private_display, "_NET_WM_WINDOW_TYPE", 0);
    dialog_atom = api.intern_atom(private_display, "_NET_WM_WINDOW_TYPE_DIALOG", 0);
    transient_atom = api.intern_atom(private_display, "WM_TRANSIENT_FOR", 0);
    name_atom = api.intern_atom(private_display, "_NET_WM_NAME", 0);
    utf8_atom = api.intern_atom(private_display, "UTF8_STRING", 0);
    begin_requests();
    {
        char *name = NULL;
        if (window_name(main_window, &name) < 0) {
            (void)end_requests();
            api.close_display(private_display);
            private_display = NULL;
            return 0;
        }
        if (name != NULL) {
            snprintf(target_name, sizeof(target_name), "%s", name);
            api.free_data(name);
        }
    }
    main_pid = property_scalar(main_window, pid_atom, XA_CARDINAL);
    if (!api.composite_version(private_display, &major, &minor) ||
        (major == 0 && minor < 2) || main_pid == 0 || main_pid > INT_MAX ||
        !matching_class(main_window)) {
        (void)end_requests();
        api.close_display(private_display);
        private_display = NULL;
        return 0;
    }
    if (!end_requests()) {
        api.close_display(private_display);
        private_display = NULL;
        return 0;
    }
    return 1;
}

static int target_display(MCDisplay *display)
{
    const char *name;
    if (!configured || display == NULL || api.display_string == NULL)
        return 0;
    name = api.display_string(display);
    return name != NULL && strcmp(name, expected_display) == 0;
}

/* A successful replacement holds the mutex until the original getter finishes,
 * so a concurrent resize cannot free the server Pixmap underneath that read. */
static int replacement_drawable(MCDisplay *display, MCDrawable drawable, MCDrawable *replacement)
{
    uint64_t now;
    pthread_once(&once, initialize);
    if (!target_display(display) || drawable != api.default_root(display))
        return 0;
    pthread_mutex_lock(&mutex);
    if (!available || !prepare_private_display()) {
        pthread_mutex_unlock(&mutex);
        return -1;
    }
    now = monotonic_ns();
    if (canvas == 0 || last_refresh == 0 || now == 0 || now - last_refresh >= refresh_ns)
        (void)refresh_canvas();
    if (canvas == 0 || canvas_picture == 0) {
        pthread_mutex_unlock(&mutex);
        return -1;
    }
    *replacement = canvas;
    return 1;
}

MCImage *XGetImage(MCDisplay *display, MCDrawable drawable, int x, int y,
                   unsigned int width, unsigned int height, unsigned long planes, int format)
{
    MCDrawable replacement = drawable;
    int selected = replacement_drawable(display, drawable, &replacement);
    MCImage *image;
    if (selected < 0 || api.get_image == NULL)
        return NULL;
    image = api.get_image(display, replacement, x, y, width, height, planes, format);
    if (selected > 0)
        pthread_mutex_unlock(&mutex);
    return image;
}

MCImage *XGetSubImage(MCDisplay *display, MCDrawable drawable, int x, int y,
                      unsigned int width, unsigned int height, unsigned long planes, int format,
                      MCImage *destination, int destination_x, int destination_y)
{
    MCDrawable replacement = drawable;
    int selected = replacement_drawable(display, drawable, &replacement);
    MCImage *image;
    if (selected < 0 || api.get_sub_image == NULL)
        return NULL;
    image = api.get_sub_image(display, replacement, x, y, width, height, planes, format,
                              destination, destination_x, destination_y);
    if (selected > 0)
        pthread_mutex_unlock(&mutex);
    return image;
}

int XShmGetImage(MCDisplay *display, MCDrawable drawable, MCImage *image,
                 int x, int y, unsigned long planes)
{
    MCDrawable replacement = drawable;
    int selected = replacement_drawable(display, drawable, &replacement);
    int result;
    if (selected < 0 || api.shm_get_image == NULL)
        return 0;
    result = api.shm_get_image(display, replacement, image, x, y, planes);
    if (selected > 0)
        pthread_mutex_unlock(&mutex);
    return result;
}

/* 0: unrelated display (pass through); 1: owned GUI; -1: suppress manager input. */
static int focus_for_input(MCDisplay *display, int motion, int x, int y, int keyboard)
{
    struct gui_window *target = NULL;
    MCWindowAttributes attributes;
    MCWindow ignored_root, ignored_child;
    int ignored_x, ignored_y;
    unsigned int ignored_mask;
    struct gui_window main;
    int collected;
    int routed = -1;
    uint64_t now;
    pthread_once(&once, initialize);
    if (!target_display(display))
        return 0;
    if (!available)
        return -1;
    pthread_mutex_lock(&mutex);
    if (!prepare_private_display() || last_status != 1)
        goto done;
    /* Revalidate hit geometry without repainting the image cache. High-rate
     * pointer events must not defeat the independent 15 FPS capture budget. */
    now = monotonic_ns();
    if (!motion || window_count == 0 || now == 0 || now - last_geometry >= refresh_ns) {
        begin_requests();
        collected = collect_windows(&main);
        if (!end_requests())
            collected = 0;
        if (!collected)
            goto done;
    }
    if (motion) {
        pointer_x = x;
        pointer_y = y;
        pointer_known = 1;
    } else if (!pointer_known && !keyboard) {
        if (api.query_pointer(private_display, root, &ignored_root, &ignored_child,
                              &pointer_x, &pointer_y, &ignored_x, &ignored_y, &ignored_mask))
            pointer_known = 1;
    }
    if (controller_role && !keyboard &&
        (!api.attributes(private_display, root, &attributes) ||
         pointer_x < 0 || pointer_y < 0 || pointer_x >= attributes.width || pointer_y >= attributes.height))
        goto done;
    for (unsigned int index = 0; index < window_count; ++index) {
        struct gui_window *entry = &windows[index];
        if (keyboard) {
            if ((target == NULL && entry->client == main_window) ||
                (entry->client == last_input_window && entry->width >= 64 && entry->height >= 64 &&
                 (target == NULL || !target->modal)) ||
                (entry->modal && entry->width >= 64 && entry->height >= 64))
                target = entry;
        } else if (pointer_known && pointer_x >= entry->x && pointer_y >= entry->y &&
                   pointer_x < entry->x + entry->width && pointer_y < entry->y + entry->height)
            target = entry;
    }
    if (target == NULL)
        goto done;
    begin_requests();
    if (same_owner(target->client) && api.attributes(private_display, target->client, &attributes) &&
        attributes.map_state == VIEWABLE) {
        int actual_x, actual_y;
        MCWindow child;
        if (keyboard || (api.translate(private_display, target->client, root, 0, 0,
                                        &actual_x, &actual_y, &child) &&
                         pointer_x >= actual_x && pointer_y >= actual_y &&
                         pointer_x < actual_x + attributes.width &&
                         pointer_y < actual_y + attributes.height))
            routed = 1;
    }
    if (!end_requests())
        routed = -1;
    if (routed == 1) {
        if (input_trace_remaining != 0) {
            MCWindow focus = 0, pointer_root = 0, pointer_child = 0;
            int revert = 0, actual_x = 0, actual_y = 0, local_x = 0, local_y = 0;
            unsigned int mask = 0;
            int focus_ok = api.get_input_focus(display, &focus, &revert);
            int pointer_ok = api.query_pointer(display, root, &pointer_root, &pointer_child,
                                               &actual_x, &actual_y, &local_x, &local_y, &mask);
            log_message("input target=%lu frame=%lu override=%d motion=%d keyboard=%d "
                        "requested=%d,%d pointer_ok=%d actual=%d,%d child=%lu "
                        "focus_ok=%d focus=%lu revert=%d policy=%s",
                        target->client, target->frame, attributes.override_redirect, motion, keyboard,
                        pointer_x, pointer_y, pointer_ok, actual_x, actual_y, pointer_child,
                        focus_ok, focus, revert, attributes.override_redirect ? "preserve-popup" :
                        isolated_manager && motion ? "preserve-motion-focus" : "focus-managed");
            --input_trace_remaining;
        }
        /* An override-redirect menu keeps its existing focus/grab owner.
         * Focusing its temporary client can terminate Wine's menu loop before
         * the following XTest press. Managed main/modal windows still focus. */
        if (!attributes.override_redirect && (!isolated_manager || !motion)) {
            api.raise_window(display, target->frame);
            api.set_input_focus(display, target->client, 1, 0);
            last_input_window = target->client;
        }
    }
done:
    pthread_mutex_unlock(&mutex);
    return routed;
}

/* Releases accepted by this sidecar must still reach the server after the GUI
 * disappears. New presses remain blocked; no other client's state is released. */
static int press_event(MCDisplay *display, void *device, unsigned int code, int pressed,
                       int *axes, int count, unsigned long delay, int button)
{
    unsigned long device_id = 0;
    unsigned int index;
    int kind = (device != NULL ? 2 : 0) + button;
    int routed = focus_for_input(display, 0, 0, 0, !button);
    int result = 0;
    /* XDevice's first public ABI member is its XID. Never retain its pointer:
     * the caller can close the device before this library's destructor. */
    if (device != NULL)
        memcpy(&device_id, device, sizeof(device_id));
    pthread_mutex_lock(&mutex);
    for (index = 0; index < press_count; ++index)
        if (presses[index].kind == kind && presses[index].device == device_id && presses[index].code == code)
            break;
    if (routed < 0 && (pressed || index == press_count))
        goto done;
    if (routed > 0 && pressed && index == press_count && press_count == MAX_PRESSES)
        goto done;
    if (button && routed > 0 && (device == NULL || !axes) && api.fake_motion != NULL)
        api.fake_motion(display, -1, pointer_x, pointer_y, 0);
    if (device != NULL) {
        if (button && api.fake_device_button != NULL)
            result = api.fake_device_button(display, device, code, pressed, axes, count, delay);
        else if (!button && api.fake_device_key != NULL)
            result = api.fake_device_key(display, device, code, pressed, axes, count, delay);
    } else if (button && api.fake_button != NULL)
        result = api.fake_button(display, code, pressed, delay);
    else if (!button && api.fake_key != NULL)
        result = api.fake_key(display, code, pressed, delay);
    if (routed != 0 && result) {
        if (pressed && index == press_count)
            presses[press_count++] = (struct accepted_press){device_id, code, kind};
        else if (!pressed && index < press_count)
            presses[index] = presses[--press_count];
    }
done:
    pthread_mutex_unlock(&mutex);
    return result;
}

int XTestFakeKeyEvent(MCDisplay *display, unsigned int key, int pressed, unsigned long delay)
{
    return press_event(display, NULL, key, pressed, NULL, 0, delay, 0);
}

int XTestFakeButtonEvent(MCDisplay *display, unsigned int button, int pressed, unsigned long delay)
{
    /* VNC may omit a redundant motion. A controller can meanwhile have moved
     * the shared server pointer; restore the manager's last requested position. */
    return press_event(display, NULL, button, pressed, NULL, 0, delay, 1);
}

int XTestFakeMotionEvent(MCDisplay *display, int screen, int x, int y, unsigned long delay)
{
    if (focus_for_input(display, 1, x, y, 0) < 0)
        return 0;
    return api.fake_motion != NULL ? api.fake_motion(display, screen, x, y, delay) : 0;
}

int XTestFakeDeviceKeyEvent(MCDisplay *display, void *device, unsigned int key, int pressed,
                           int *axes, int count, unsigned long delay)
{
    return press_event(display, device, key, pressed, axes, count, delay, 0);
}

int XTestFakeDeviceButtonEvent(MCDisplay *display, void *device, unsigned int button, int pressed,
                              int *axes, int count, unsigned long delay)
{
    return press_event(display, device, button, pressed, axes, count, delay, 1);
}

int XTestFakeDeviceMotionEvent(MCDisplay *display, void *device, int relative, int first_axis,
                              int *axes, int count, unsigned long delay)
{
    int routed;
    if (!relative && first_axis == 0 && axes != NULL && count >= 2)
        routed = focus_for_input(display, 1, axes[0], axes[1], 0);
    else
        routed = focus_for_input(display, 0, 0, 0, 0);
    if (routed < 0)
        return 0;
    return api.fake_device_motion != NULL ? api.fake_device_motion(display, device, relative, first_axis, axes, count, delay) : 0;
}

__attribute__((constructor)) static void startup(void)
{
    pthread_once(&once, initialize);
    if (!configured)
        return;
    pthread_mutex_lock(&mutex);
    if (!available || !prepare_private_display() || !refresh_canvas() || last_status != 1) {
        log_message("initial Composite capture failed; ending management sidecar");
        _exit(70);
    }
    pthread_mutex_unlock(&mutex);
}

__attribute__((destructor)) static void cleanup(void)
{
    pthread_mutex_lock(&mutex);
    if (private_display != NULL) {
        begin_requests();
        for (unsigned int index = 0; index < press_count; ++index) {
            const struct accepted_press *press = &presses[index];
            if (press->kind == 0)
                api.fake_key(private_display, press->code, 0, 0);
            else if (press->kind == 1)
                api.fake_button(private_display, press->code, 0, 0);
            else {
                void *device = api.open_device(private_display, press->device);
                if (device != NULL) {
                    if (press->kind == 2)
                        api.fake_device_key(private_display, device, press->code, 0, NULL, 0, 0);
                    else
                        api.fake_device_button(private_display, device, press->code, 0, NULL, 0, 0);
                    api.close_device(private_display, device);
        }
    }
        }
        press_count = 0;
        for (unsigned int index = 0; index < redirected_count; ++index)
            api.unredirect_window(private_display, redirected_frames[index], isolated_manager ? 1 : 0);
        redirected_count = 0;
        if (canvas_picture != 0)
            api.free_picture(private_display, canvas_picture);
        if (canvas != 0)
            api.free_pixmap(private_display, canvas);
        (void)end_requests();
        api.close_display(private_display);
        private_display = NULL;
    }
    if (log_file != NULL)
        fclose(log_file);
    pthread_mutex_unlock(&mutex);
}
