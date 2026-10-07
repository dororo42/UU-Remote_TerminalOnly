#define _GNU_SOURCE
#include "../../src/x11_manager_capture_api.h"
#include <assert.h>
#include <dlfcn.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Image layout is used only by this real MIT-SHM fixture. Production keeps it opaque. */
struct _XImage {
    int width, height, xoffset, format;
    char *data;
    int byte_order, bitmap_unit, bitmap_bit_order, bitmap_pad, depth, bytes_per_line, bits_per_pixel;
    unsigned long red_mask, green_mask, blue_mask;
    void *obdata;
    void *functions[6];
};
typedef struct {
    unsigned long shmseg;
    int shmid;
    char *shmaddr;
    int read_only;
} ShmInfo;
typedef struct {
    MCVisual *visual;
    unsigned long visualid;
    int screen, depth, visual_class;
    unsigned long red_mask, green_mask, blue_mask;
    int colormap_size, bits_per_rgb;
} VisualInfo;
typedef struct {
    unsigned long background_pixmap, background_pixel, border_pixmap, border_pixel;
    int bit_gravity, win_gravity, backing_store;
    unsigned long backing_planes, backing_pixel;
    int save_under;
    long event_mask, do_not_propagate_mask;
    int override_redirect;
    unsigned long colormap, cursor;
} SetAttributes;
typedef struct {
    int type;
    unsigned long serial;
    int send_event;
    MCDisplay *display;
    MCWindow window;
} AnyEvent;
typedef struct {
    AnyEvent any;
    MCAtom message_type;
    int format;
    union { char bytes[20]; short shorts[10]; long longs[5]; } data;
} MessageEvent;
typedef struct { unsigned char *value; MCAtom encoding; int format; unsigned long nitems; } TextProperty;
typedef struct {
    AnyEvent any;
    MCWindow root, subwindow;
    unsigned long time;
    int x, y, x_root, y_root;
    unsigned int state, button;
    int same_screen;
} ButtonEvent;
typedef union { AnyEvent any; MessageEvent message; ButtonEvent button; long padding[24]; } Event;

struct fixture_api {
    MCDisplay *(*open)(const char *);
    int (*close)(MCDisplay *);
    MCWindow (*root)(MCDisplay *);
    int (*screen)(MCDisplay *);
    int (*depth)(MCDisplay *, int);
    MCVisual *(*visual)(MCDisplay *, int);
    MCWindow (*simple)(MCDisplay *, MCWindow, int, int, unsigned int, unsigned int, unsigned int, unsigned long, unsigned long);
    MCWindow (*create)(MCDisplay *, MCWindow, int, int, unsigned int, unsigned int, unsigned int, int, unsigned int, MCVisual *, unsigned long, SetAttributes *);
    unsigned long (*colormap)(MCDisplay *, MCWindow, MCVisual *, int);
    int (*match_visual)(MCDisplay *, int, int, int, VisualInfo *);
    int (*class_hint)(MCDisplay *, MCWindow, MCClassHint *);
    int (*name)(MCDisplay *, MCWindow, const char *);
    int (*fetch_name)(MCDisplay *, MCWindow, char **);
    int (*utf8_text)(MCDisplay *, char **, int, int, TextProperty *);
    int (*free_data)(void *);
    MCAtom (*atom)(MCDisplay *, const char *, int);
    int (*property)(MCDisplay *, MCWindow, MCAtom, MCAtom, int, int, const unsigned char *, int);
    int (*delete_property)(MCDisplay *, MCWindow, MCAtom);
    int (*change_attributes)(MCDisplay *, MCWindow, unsigned long, SetAttributes *);
    int (*attributes)(MCDisplay *, MCWindow, MCWindowAttributes *);
    int (*translate)(MCDisplay *, MCWindow, MCWindow, int, int, int *, int *, MCWindow *);
    int (*clear)(MCDisplay *, MCWindow);
    int (*map)(MCDisplay *, MCWindow);
    int (*unmap)(MCDisplay *, MCWindow);
    int (*raise)(MCDisplay *, MCWindow);
    int (*move)(MCDisplay *, MCWindow, int, int);
    int (*resize)(MCDisplay *, MCWindow, unsigned int, unsigned int);
    int (*destroy)(MCDisplay *, MCWindow);
    int (*select)(MCDisplay *, MCWindow, long);
    int (*sync)(MCDisplay *, int);
    int (*focus)(MCDisplay *, MCWindow *, int *);
    int (*set_focus)(MCDisplay *, MCWindow, int, unsigned long);
    int (*pending)(MCDisplay *);
    int (*event)(MCDisplay *, Event *);
    int (*warp)(MCDisplay *, MCWindow, MCWindow, int, int, unsigned int, unsigned int, int, int);
    int (*keymap)(MCDisplay *, char *);
    int (*pointer)(MCDisplay *, MCWindow, MCWindow *, MCWindow *, int *, int *, int *, int *, unsigned int *);
    MCImage *(*image)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int);
    unsigned long (*pixel)(MCImage *, int, int);
    int (*destroy_image)(MCImage *);
    MCImage *(*shm_create)(MCDisplay *, MCVisual *, unsigned int, int, char *, ShmInfo *, unsigned int, unsigned int);
    int (*shm_attach)(MCDisplay *, ShmInfo *);
    int (*shm_detach)(MCDisplay *, ShmInfo *);
    MCRenderFormat *(*format)(MCDisplay *, MCVisual *);
    MCPicture (*picture)(MCDisplay *, MCDrawable, const MCRenderFormat *, unsigned long, const void *);
    void (*fill)(MCDisplay *, int, MCPicture, const MCRenderColor *, int, int, unsigned int, unsigned int);
    void (*free_picture)(MCDisplay *, MCPicture);
};
static struct fixture_api x;
static MCDisplay *display;
static MCWindow root, frame, main_window, relay;
static unsigned long main_pid;
static void *render_library;
static volatile sig_atomic_t stop;

static void symbol(void *library, const char *name, void *destination, size_t size)
{
    void *address = dlsym(library, name);
    assert(address != NULL && size == sizeof(address));
    memcpy(destination, &address, size);
}
#define GET(library, field, name) symbol(library, name, &x.field, sizeof(x.field))

static void wait_server(void)
{
    struct timespec delay = {0, 100000000};
    x.sync(display, 0);
    nanosleep(&delay, NULL);
}

static void owner(MCWindow window, const char *class_name, unsigned long pid)
{
    MCClassHint hint = {(char *)class_name, (char *)class_name};
    x.class_hint(display, window, &hint);
    x.property(display, window, x.atom(display, "_NET_WM_PID", 0), 6, 32, 0,
                (unsigned char *)&pid, 1);
}

static void wine_unicode_title(MCWindow window, const char *title)
{
    TextProperty property;
    char *text = (char *)title, *legacy = NULL;
    assert(x.utf8_text(display, &text, 1, 1, &property) >= 0);
    assert(property.encoding == x.atom(display, "COMPOUND_TEXT", 0));
    x.property(display, window, x.atom(display, "WM_NAME", 0), property.encoding,
               property.format, 0, property.value, (int)property.nitems);
    x.free_data(property.value);
    x.property(display, window, x.atom(display, "_NET_WM_NAME", 0),
               x.atom(display, "UTF8_STRING", 0), 8, 0,
               (const unsigned char *)title, (int)strlen(title));
    x.sync(display, 0);
    assert(!x.fetch_name(display, window, &legacy) && legacy == NULL);
}

static unsigned long pixel(MCImage *image, int px, int py)
{
    assert(image != NULL);
    return x.pixel(image, px, py) & 0xffffffUL;
}

static void assert_root_pixel(void *module, int px, int py, unsigned long expected)
{
    MCImage *(*getter)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int);
    MCImage *image;
    symbol(module, "XGetImage", &getter, sizeof(getter));
    image = getter(display, root, 0, 0, 800, 600, ~0UL, 2);
    assert(pixel(image, px, py) == expected);
    x.destroy_image(image);
}

static void terminate_fixture(int signal_number) { (void)signal_number; stop = 1; }


static void foreign_churn(const char *ready)
{
    char path[4096];
    assert(snprintf(path, sizeof(path), "%s.foreign", ready) < (int)sizeof(path));
    FILE *ranges = fopen(path, "wb");
    assert(ranges);
    signal(SIGTERM, terminate_fixture);
    while (!stop) {
        MCWindow foreign[64];
        for (unsigned int index = 0; index < 64; ++index) {
            foreign[index] = x.simple(display, root, 400, 400, 10, 10, 0, 0, 0xffffff);
            owner(foreign[index], "foreign-churn", (unsigned long)getpid());
            if (index != 0)
                assert(foreign[index] == foreign[0] + index);
        }
        unsigned long range[2] = {foreign[0], foreign[63]};
        assert(fwrite(range, sizeof(range), 1, ranges) == 1);
        x.sync(display, 0);
        for (unsigned int index = 0; index < 64; ++index)
            x.destroy(display, foreign[index]);
        x.sync(display, 0);
    }
    assert(fclose(ranges) == 0);
}

static void test_foreign_churn(void *module, const char *self, const char *library,
                               const char *ready)
{
    MCImage *(*getter)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int,
                       unsigned long, int);
    int normal = 0, black = 0, status, revert;
    MCWindow focused;
    symbol(module, "XGetImage", &getter, sizeof(getter));
    pid_t producer = fork();
    assert(producer >= 0);
    if (producer == 0) {
        execl(self, self, library, "foreign-worker", ready, (char *)NULL);
        _exit(127);
    }
    struct timespec delay = {0, 70000000};
    for (unsigned int sample = 0; sample < 90; ++sample) {
        nanosleep(&delay, NULL);
        MCImage *image = getter(display, root, 0, 0, 800, 600, ~0UL, 2);
        unsigned long value = pixel(image, 80, 80);
        x.destroy_image(image);
        assert(value == 0 || value == 0x00ff00);
        if (value == 0)
            ++black;
        else
            ++normal;
        image = x.image(display, main_window, 0, 0, 100, 100, ~0UL, 2);
        assert(pixel(image, 20, 20) == 0x00ff00);
        x.destroy_image(image);
        image = x.image(display, root, 0, 0, 100, 100, ~0UL, 2);
        assert(pixel(image, 80, 80) == 0x0000ff);
        x.destroy_image(image);
        x.focus(display, &focused, &revert);
        assert(focused == relay);
    }
    assert(kill(producer, SIGTERM) == 0);
    assert(waitpid(producer, &status, 0) == producer && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    wait_server();
    assert_root_pixel(module, 80, 80, 0x00ff00);
    printf("foreign churn normal=%d black=%d\n", normal, black);
    if (getenv("FIXTURE_EXPECT_FOREIGN_BLANK"))
        assert(black > 0);
    else
        assert(normal == 90 && black == 0);
}

static void load_api(void)
{
    void *xlib = dlopen("libX11.so.6", RTLD_NOW | RTLD_LOCAL);
    void *xext = dlopen("libXext.so.6", RTLD_NOW | RTLD_LOCAL);
    render_library = dlopen("libXrender.so.1", RTLD_NOW | RTLD_LOCAL);
    assert(xlib && xext && render_library);
    GET(xlib, open, "XOpenDisplay"); GET(xlib, close, "XCloseDisplay");
    GET(xlib, root, "XDefaultRootWindow"); GET(xlib, screen, "XDefaultScreen");
    GET(xlib, depth, "XDefaultDepth"); GET(xlib, visual, "XDefaultVisual");
    GET(xlib, simple, "XCreateSimpleWindow"); GET(xlib, create, "XCreateWindow");
    GET(xlib, colormap, "XCreateColormap"); GET(xlib, match_visual, "XMatchVisualInfo");
    GET(xlib, class_hint, "XSetClassHint"); GET(xlib, atom, "XInternAtom");
    GET(xlib, name, "XStoreName");
    GET(xlib, fetch_name, "XFetchName"); GET(xlib, utf8_text, "Xutf8TextListToTextProperty");
    GET(xlib, free_data, "XFree");
    GET(xlib, property, "XChangeProperty"); GET(xlib, map, "XMapWindow");
    GET(xlib, delete_property, "XDeleteProperty"); GET(xlib, change_attributes, "XChangeWindowAttributes");
    GET(xlib, translate, "XTranslateCoordinates");
    GET(xlib, attributes, "XGetWindowAttributes");
    GET(xlib, clear, "XClearWindow");
    GET(xlib, unmap, "XUnmapWindow"); GET(xlib, raise, "XRaiseWindow");
    GET(xlib, move, "XMoveWindow"); GET(xlib, resize, "XResizeWindow");
    GET(xlib, destroy, "XDestroyWindow"); GET(xlib, select, "XSelectInput");
    GET(xlib, sync, "XSync"); GET(xlib, focus, "XGetInputFocus");
    GET(xlib, set_focus, "XSetInputFocus"); GET(xlib, pending, "XPending");
    GET(xlib, event, "XNextEvent"); GET(xlib, warp, "XWarpPointer");
    GET(xlib, keymap, "XQueryKeymap"); GET(xlib, pointer, "XQueryPointer");
    GET(xlib, image, "XGetImage"); GET(xlib, pixel, "XGetPixel");
    GET(xlib, destroy_image, "XDestroyImage"); GET(xext, shm_create, "XShmCreateImage");
    GET(xext, shm_attach, "XShmAttach"); GET(xext, shm_detach, "XShmDetach");
    GET(render_library, format, "XRenderFindVisualFormat");
    GET(render_library, picture, "XRenderCreatePicture");
    GET(render_library, fill, "XRenderFillRectangle");
    GET(render_library, free_picture, "XRenderFreePicture");
}

static void test_getters(void *module)
{
    MCImage *(*sub)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int, MCImage *, int, int);
    int (*shm)(MCDisplay *, MCDrawable, MCImage *, int, int, unsigned long);
    MCImage *image = x.image(display, relay, 0, 0, 200, 160, ~0UL, 2);
    ShmInfo info = {0, -1, NULL, 0};
    MCImage *shared;
    symbol(module, "XGetSubImage", &sub, sizeof(sub));
    symbol(module, "XShmGetImage", &shm, sizeof(shm));
    assert(sub(display, root, 60, 60, 100, 100, ~0UL, 2, image, 10, 10) == image);
    assert(pixel(image, 20, 20) == 0x00ff00);
    assert(pixel(image, 0, 0) == 0x0000ff);
    x.destroy_image(image);
    shared = x.shm_create(display, x.visual(display, x.screen(display)),
                          (unsigned int)x.depth(display, x.screen(display)), 2, NULL, &info, 100, 100);
    assert(shared);
    info.shmid = shmget(IPC_PRIVATE, (size_t)shared->bytes_per_line * 100U, IPC_CREAT | 0600);
    assert(info.shmid >= 0);
    info.shmaddr = shmat(info.shmid, NULL, 0);
    assert(info.shmaddr != (void *)-1);
    shared->data = info.shmaddr;
    assert(x.shm_attach(display, &info));
    x.sync(display, 0);
    assert(shmctl(info.shmid, IPC_RMID, NULL) == 0);
    assert(shm(display, root, shared, 60, 60, ~0UL));
    assert(pixel(shared, 20, 20) == 0x00ff00);
    assert(x.shm_detach(display, &info));
    x.sync(display, 0);
    shared->data = NULL;
    x.destroy_image(shared);
    assert(shmdt(info.shmaddr) == 0);
}

static MCWindow make_modal(int alpha)
{
    MCWindow window;
    MCAtom type;
    if (alpha) {
        VisualInfo visual;
        SetAttributes attributes = {0};
        MCRenderFormat *format;
        MCPicture picture;
        MCRenderColor transparent = {0, 0, 0, 0};
        MCRenderColor half_red = {32768, 0, 0, 32768};
        MCRenderColor red = {65535, 0, 0, 65535};
        assert(x.match_visual(display, x.screen(display), 32, 4, &visual));
        format = x.format(display, visual.visual);
        assert(format && format->direct.alpha_mask != 0);
        attributes.colormap = x.colormap(display, root, visual.visual, 0);
        attributes.override_redirect = 1;
        window = x.create(display, root, 100, 90, 120, 90, 0, 32, 1, visual.visual,
                           (1UL << 1) | (1UL << 3) | (1UL << 9) | (1UL << 13), &attributes);
        x.map(display, window);
        picture = x.picture(display, window, format, 0, NULL);
        x.fill(display, 1, picture, &transparent, 0, 0, 120, 90);
        x.fill(display, 1, picture, &half_red, 40, 0, 40, 90);
        x.fill(display, 1, picture, &red, 80, 0, 40, 90);
        x.free_picture(display, picture);
        type = x.atom(display, "_NET_WM_WINDOW_TYPE_DIALOG", 0);
    } else {
        window = x.simple(display, root, 100, 90, 120, 90, 0, 0, 0xff0000);
        type = x.atom(display, "_NET_WM_WINDOW_TYPE_NORMAL", 0);
        x.map(display, window);
    }
    owner(window, "gameviewer.exe", main_pid);
    x.property(display, window, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
                (unsigned char *)&type, 1);
    if (alpha)
        x.property(display, window, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
                    (unsigned char *)&main_window, 1);
    x.select(display, window, (1L << 0) | (1L << 2));
    wait_server();
    return window;
}

static void assert_press_state(int expected)
{
    char keys[32];
    MCWindow returned_root, child;
    int root_x, root_y, window_x, window_y;
    unsigned int mask;
    x.sync(display, 0);
    assert(x.keymap(display, keys));
    assert(((keys[38 / 8] >> (38 % 8)) & 1) == expected);
    assert(x.pointer(display, root, &returned_root, &child, &root_x, &root_y,
                     &window_x, &window_y, &mask));
    assert(!!(mask & (1U << 8)) == expected);
}

static void test_input(void *module, MCWindow modal)
{
    int (*motion)(MCDisplay *, int, int, int, unsigned long);
    int (*button)(MCDisplay *, unsigned int, int, unsigned long);
    int (*key)(MCDisplay *, unsigned int, int, unsigned long);
    MCWindow focused;
    int revert;
    int received = 0;
    unsigned long *composites = dlsym(render_library, "manager_test_composites");
    unsigned long before;
    assert(composites);
    symbol(module, "XTestFakeMotionEvent", &motion, sizeof(motion));
    symbol(module, "XTestFakeButtonEvent", &button, sizeof(button));
    symbol(module, "XTestFakeKeyEvent", &key, sizeof(key));
    before = *composites;
    for (int index = 0; index < 100; ++index)
        assert(motion(display, -1, 80, 80, 0));
    assert(*composites == before);  /* Input never repaints the cached canvas. */
    x.sync(display, 0);
    x.warp(display, 0, root, 0, 0, 0, 0, 700, 500);
    x.raise(display, relay);
    x.set_focus(display, relay, 1, 0);
    x.sync(display, 0);
    assert(button(display, 1, 1, 0));
    assert(button(display, 1, 0, 0));
    x.sync(display, 0);
    while (x.pending(display)) {
        Event event;
        x.event(display, &event);
        if (event.any.type == 4 && event.any.window == main_window)
            ++received;
        assert(!(event.any.type == 4 && event.any.window == relay));
    }
    assert(received == 1);  /* Cached manager P restores remote-warped Q. */
    if (modal != 0) {
        MCWindowAttributes attributes;
        MCWindow prior_focus;
        int popup_presses = 0;
        assert(x.attributes(display, modal, &attributes));
        x.focus(display, &prior_focus, &revert);
        assert(prior_focus == main_window);
        x.raise(display, modal);  /* App presents its independent popup. */
        wait_server();
        assert(motion(display, -1, 110, 110, 0));
        assert(button(display, 1, 1, 0));
        assert(button(display, 1, 0, 0));
        assert(key(display, 38, 1, 0));
        assert(key(display, 38, 0, 0));
        x.sync(display, 0);
        x.focus(display, &focused, &revert);
        assert(focused == (attributes.override_redirect ? prior_focus : modal));
        while (x.pending(display)) {
            Event event;
            x.event(display, &event);
            if (event.any.type == 4) {
                assert(event.any.window == modal);
                assert(event.button.button == 1);
                assert(event.button.x == 10 && event.button.y == 20);
                assert(event.button.x_root == 110 && event.button.y_root == 110);
                ++popup_presses;
            }
        }
        assert(popup_presses == 1);
    }
    assert(button(display, 1, 1, 0));
    assert(key(display, 38, 1, 0));
    assert_press_state(1);
    x.unmap(display, main_window);
    wait_server();
    assert_root_pixel(module, 80, 80, 0);
    x.set_focus(display, relay, 1, 0);
    x.sync(display, 0);
    assert(!button(display, 1, 1, 0));
    assert(!motion(display, -1, 80, 80, 0));
    assert(!key(display, 39, 1, 0));
    assert(button(display, 1, 0, 0));
    assert(key(display, 38, 0, 0));
    assert_press_state(0);
    assert(!button(display, 1, 0, 0));
    assert(!key(display, 38, 0, 0));
    x.sync(display, 0);
    x.focus(display, &focused, &revert);
    assert(focused == relay);
}

static void test_cleanup_input(void *module)
{
    int (*motion)(MCDisplay *, int, int, int, unsigned long);
    int (*button)(MCDisplay *, unsigned int, int, unsigned long);
    int (*key)(MCDisplay *, unsigned int, int, unsigned long);
    symbol(module, "XTestFakeMotionEvent", &motion, sizeof(motion));
    symbol(module, "XTestFakeButtonEvent", &button, sizeof(button));
    symbol(module, "XTestFakeKeyEvent", &key, sizeof(key));
    assert(motion(display, -1, 80, 80, 0));
    assert(button(display, 1, 1, 0));
    assert(key(display, 38, 1, 0));
    assert_press_state(1);
    x.unmap(display, main_window);
    wait_server();
    dlclose(module);
    assert_press_state(0);
}

static void test_isolated_input(void *module)
{
    int (*motion)(MCDisplay *, int, int, int, unsigned long);
    int (*button)(MCDisplay *, unsigned int, int, unsigned long);
    int (*key)(MCDisplay *, unsigned int, int, unsigned long);
    MCWindow focused;
    MCWindowAttributes attributes;
    int revert, clicks = 0, keys = 0;
    symbol(module, "XTestFakeMotionEvent", &motion, sizeof(motion));
    symbol(module, "XTestFakeButtonEvent", &button, sizeof(button));
    symbol(module, "XTestFakeKeyEvent", &key, sizeof(key));
    /* The mapped manager may raise itself; the real desktop still shows SDL.
     * Its separate capture remains readable without a root compositor. */
    x.raise(display, frame);
    wait_server();
    for (int index = 0; index < 8; ++index) {
        MCImage *image = x.image(display, root, 80, 80, 1, 1, ~0UL, 2);
        assert(pixel(image, 0, 0) == 0x0000ff);
        x.destroy_image(image);
        assert_root_pixel(module, 80, 80, 0x00ff00);
        assert(x.attributes(display, main_window, &attributes) && attributes.map_state == 2);
        x.set_focus(display, relay, 1, 0);
        assert(motion(display, -1, 80, 80, 0));
        x.sync(display, 0);
        x.focus(display, &focused, &revert);
        assert(focused == relay);  /* Hover cannot steal desktop input. */
        assert(button(display, 1, 1, 0));
        x.set_focus(display, relay, 1, 0);  /* A competing supervisor tick. */
        assert(button(display, 1, 0, 0));
        x.set_focus(display, relay, 1, 0);
        assert(key(display, 38, 1, 0));
        x.set_focus(display, relay, 1, 0);
        assert(key(display, 38, 0, 0));
        x.sync(display, 0);
        while (x.pending(display)) {
            Event event;
            x.event(display, &event);
            if (event.any.type == 4 || event.any.type == 2) {
                assert(event.any.window == main_window);
                clicks += event.any.type == 4;
                keys += event.any.type == 2;
            }
        }
    }
    assert(clicks == 8 && keys == 8);
    dlclose(module);
    x.clear(display, main_window);
    wait_server();
    MCImage *image = x.image(display, root, 80, 80, 1, 1, ~0UL, 2);
    assert(pixel(image, 0, 0) == 0x00ff00);  /* Cleanup releases its redirect. */
    x.destroy_image(image);
}

static void assert_large_pixel(void *module, int px, int py, unsigned long expected)
{
    MCImage *(*getter)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int);
    MCImage *image;
    symbol(module, "XGetImage", &getter, sizeof(getter));
    image = getter(display, root, px, py, 1, 1, ~0UL, 2);
    assert(pixel(image, 0, 0) == expected);
    x.destroy_image(image);
}

static void test_controller(const char *library, const char *mode, const char *ready)
{
    MCWindow lobby, independent, modal, nested, focused;
    MCAtom type = x.atom(display, "_NET_WM_WINDOW_TYPE_NORMAL", 0);
    int revert;
    int serving = !strncmp(mode, "serve-controller", 16);
    int serve_popup = strstr(mode, "serve-controller-popup") == mode;
    int unicode_titles = strstr(mode, "utf8") != NULL;
    char window_id[32];
    void *module;
    lobby = x.simple(display, root, 1460, 740, 920, 680, 0, 0, 0xff0000);
    owner(lobby, "gameviewer.exe", main_pid);
    x.name(display, lobby, "UU Remote");
    x.property(display, lobby, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
               (unsigned char *)&type, 1);
    if (serving) {
        main_window = x.simple(display, root, 384, 196, 3072, 1768, 0, 0, 0x00ff00);
        frame = main_window;
    } else {
        frame = x.simple(display, root, 384, 196, 3072, 1768, 0, 0, 0x555555);
        main_window = x.simple(display, frame, 0, 0, 3072, 1768, 0, 0, 0x00ff00);
    }
    owner(main_window, "gameviewer.exe", main_pid);
    x.name(display, main_window, "Fixture Mac mini");
    x.property(display, main_window, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
               (unsigned char *)&type, 1);
    x.select(display, main_window, (1L << 0) | (1L << 2));
    relay = x.simple(display, root, 0, 0, 3840, 2160, 0, 0, 0x0000ff);
    owner(relay, "sdl-freerdp", main_pid);
    x.map(display, lobby); x.map(display, frame); x.map(display, main_window);
    modal = make_modal(1);
    x.name(display, modal, "GameViewer");
    x.move(display, modal, 3348, 1748);
    nested = x.simple(display, root, 600, 400, 120, 90, 0, 0, 0xffffff);
    owner(nested, "gameviewer.exe", main_pid);
    x.name(display, nested, "Nested menu");
    x.property(display, nested, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
               (unsigned char *)&modal, 1);
    x.map(display, nested);
    independent = x.simple(display, root, 800, 400, 640, 360, 0, 0, 0xffffff);
    owner(independent, "gameviewer.exe", main_pid);
    x.name(display, independent, "Other remote machine");
    x.property(display, independent, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
               (unsigned char *)&type, 1);
    x.map(display, independent);
    if (unicode_titles) {
        wine_unicode_title(main_window, "测试的Mac mini");
        wine_unicode_title(lobby, "网易UU远程");
        wine_unicode_title(independent, "另一台测试电脑");
    }
    if (strstr(mode, "controller-title-invalid") == mode) {
        unsigned char bytes[5000];
        MCAtom encoding = x.atom(display, "UTF8_STRING", 0);
        int format = 8, length = 5;
        memset(bytes, 'x', sizeof(bytes));
        if (strstr(mode, "type")) encoding = 31;
        if (strstr(mode, "format")) { format = 32; length = 1; }
        if (strstr(mode, "nul")) bytes[2] = 0;
        if (strstr(mode, "utf8")) { bytes[0] = 0xc0; bytes[1] = 0xaf; }
        if (strstr(mode, "surrogate")) { bytes[0] = 0xed; bytes[1] = 0xa0; bytes[2] = 0x80; }
        if (strstr(mode, "oversized")) length = 1024;
        if (strstr(mode, "truncated")) length = 5000;
        x.property(display, main_window, x.atom(display, "_NET_WM_NAME", 0),
                   encoding, format, 0, bytes, length);
    }
    if (!strcmp(mode, "controller-cycle"))
        x.property(display, modal, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
                   (unsigned char *)&nested, 1);
    if (!strcmp(mode, "controller-deep")) {
        MCWindow parent = main_window;
        MCAtom dialog = x.atom(display, "_NET_WM_WINDOW_TYPE_DIALOG", 0);
        for (unsigned int level = 0; level < 8; ++level) {
            MCWindow child = x.simple(display, root, 2400, 1000, 80, 80, 0, 0, 0xffffff);
            owner(child, "gameviewer.exe", main_pid);
            x.name(display, child, "Bounded transient ancestor");
            x.property(display, child, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
                       (unsigned char *)&dialog, 1);
            x.property(display, child, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
                       (unsigned char *)&parent, 1);
            x.map(display, child);
            parent = child;
        }
        x.property(display, nested, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
                   (unsigned char *)&parent, 1);
    }
    if (!strncmp(mode, "controller-generic", 18) || serve_popup) {
        SetAttributes attributes = {0};
        if (serve_popup) x.unmap(display, modal);
        x.move(display, modal, 3330, 1748);
        x.change_attributes(display, modal, 1UL << 9, &attributes);
        x.property(display, modal, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
                   (unsigned char *)&type, 1);
        x.delete_property(display, modal, x.atom(display, "WM_TRANSIENT_FOR", 0));
        if (serve_popup) {
            MCAtom above = x.atom(display, "_NET_WM_STATE_ABOVE", 0);
            x.property(display, modal, x.atom(display, "_NET_WM_STATE", 0), 4, 32, 0,
                       (unsigned char *)&above, 1);
            x.map(display, modal);
            x.unmap(display, nested);
            x.move(display, nested, 3410, 450);
            MCAtom dialog = x.atom(display, "_NET_WM_WINDOW_TYPE_DIALOG", 0);
            x.property(display, nested, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
                       (unsigned char *)&dialog, 1);
            x.property(display, nested, x.atom(display, "_NET_WM_STATE", 0), 4, 32, 0,
                       (unsigned char *)&above, 1);
            x.select(display, nested, (1L << 2) | (1L << 15));
            x.map(display, nested);
        }
        if (strstr(mode, "controller-generic-ambiguous") == mode) {
            x.move(display, independent, 384, 196);
            x.resize(display, independent, 3072, 1768);
        }
        if (!strcmp(mode, "controller-generic-640")) {
            VisualInfo visual;
            MCPicture picture;
            MCRenderColor half_red = {32768, 0, 0, 32768};
            x.move(display, modal, 1000, 1000);
            x.resize(display, modal, 640, 640);
            assert(x.match_visual(display, x.screen(display), 32, 4, &visual));
            picture = x.picture(display, modal, x.format(display, visual.visual), 0, NULL);
            x.fill(display, 1, picture, &half_red, 0, 0, 640, 640);
            x.free_picture(display, picture);
        }
    }
    if (!strcmp(mode, "controller-generic-cycle")) {
        SetAttributes attributes = {0};
        MCWindow second = make_modal(1);
        x.change_attributes(display, second, 1UL << 9, &attributes);
        x.name(display, second, "GameViewer");
        x.property(display, second, x.atom(display, "_NET_WM_WINDOW_TYPE", 0), 4, 32, 0,
                   (unsigned char *)&type, 1);
        x.property(display, second, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
                   (unsigned char *)&modal, 1);
        x.property(display, modal, x.atom(display, "WM_TRANSIENT_FOR", 0), 33, 32, 0,
                   (unsigned char *)&second, 1);
        x.move(display, second, 600, 400);
    }
    if (!strcmp(mode, "controller-generic-invalid-nul")) {
        static const unsigned char invalid[] = "GameViewer\0trailing";
        x.property(display, modal, x.atom(display, "_NET_WM_NAME", 0),
                   x.atom(display, "UTF8_STRING", 0), 8, 0, invalid, sizeof(invalid) - 1);
    }
    x.map(display, relay);
    if (serving)
        wait_server();
    x.raise(display, relay); x.set_focus(display, relay, 1, 0);
    wait_server();
    if (serving) {
        MCAtom protocol = x.atom(display, "WM_DELETE_WINDOW", 0);
        FILE *file;
        char *closed;
        assert(asprintf(&closed, "%s.closed", ready) >= 0);
        x.property(display, main_window, x.atom(display, "WM_PROTOCOLS", 0), 4, 32, 0,
                   (unsigned char *)&protocol, 1);
        wait_server();
        if (serve_popup) {
            MCWindow child;
            int px, py;
            VisualInfo visual;
            MCPicture picture;
            MCRenderColor half_red = {32768, 0, 0, 32768};
            assert(x.translate(display, main_window, root, 0, 0, &px, &py, &child));
            x.move(display, modal, px + 3072 - 150, py + 100);
            x.move(display, nested, px + 3072 - 40, py + 150);
            wait_server();
            assert(x.match_visual(display, x.screen(display), 32, 4, &visual));
            assert(x.format(display, visual.visual)->direct.alpha_mask != 0);
            picture = x.picture(display, modal, x.format(display, visual.visual), 0, NULL);
            x.fill(display, 1, picture, &half_red, 0, 0, 120, 90);
            x.free_picture(display, picture);
            x.clear(display, nested);
            wait_server();
        }
        file = fopen(ready, "w"); assert(file);
        fprintf(file, "%lu\n%lu\n%lu\n", main_window, lobby, relay);
        fclose(file);
        if (serve_popup) {
            char *popup_ids;
            assert(asprintf(&popup_ids, "%s.popups", ready) >= 0);
            file = fopen(popup_ids, "w"); assert(file);
            fprintf(file, "%lu\n%lu\n", modal, nested); fclose(file); free(popup_ids);
        }
        signal(SIGTERM, terminate_fixture);
        while (!stop) {
            struct timespec delay = {0, 10000000};
            while (x.pending(display)) {
                Event event;
                x.event(display, &event);
                if (serve_popup && event.any.type == 12 && event.any.window == nested)
                    x.clear(display, nested);
                if (serve_popup && event.any.type == 4) {
                    char *click_path;
                    assert(asprintf(&click_path, "%s.clicked", ready) >= 0);
                    file = fopen(click_path, "w"); assert(file);
                    fprintf(file, "%lu\n", nested); fclose(file); free(click_path);
                }
                if (event.any.type == 33 && event.any.window == main_window &&
                    event.message.message_type == x.atom(display, "WM_PROTOCOLS", 0) &&
                    event.message.format == 32 && (MCAtom)event.message.data.longs[0] == protocol) {
                    file = fopen(closed, "w"); assert(file);
                    fputs("WM_DELETE_WINDOW acknowledged\n", file); fclose(file);
                    x.destroy(display, main_window); x.sync(display, 0);
                }
            }
            nanosleep(&delay, NULL);
        }
        free(closed);
        return;
    }
    snprintf(window_id, sizeof(window_id), "%lu", !strcmp(mode, "role-manager") ? lobby : main_window);
    setenv("UURB_MANAGER_WINDOW_ID", window_id, 1);
    setenv("UURB_MANAGER_EXPECTED_DISPLAY", getenv("DISPLAY"), 1);
    setenv("UURB_MANAGER_CAPTURE_READY_FILE", ready, 1);
    setenv("UURB_MANAGER_CAPTURE_NONCE", "00000000-0000-4000-8000-000000000001", 1);
    if (getenv("UURB_MANAGER_CAPTURE_ROLE") == NULL)
        setenv("UURB_MANAGER_CAPTURE_ROLE", !strcmp(mode, "role-manager") ? "manager" : "controller", 1);
    module = dlopen(library, RTLD_NOW | RTLD_LOCAL);
    assert(module);
    x.focus(display, &focused, &revert); assert(focused == relay);
    if (!strcmp(mode, "role-manager")) {
        assert_large_pixel(module, 1600, 900, 0xff0000);
        assert_large_pixel(module, 3400, 1900, 0);
        assert_large_pixel(module, 650, 450, 0);
    } else {
        assert_large_pixel(module, 1600, 900, 0x00ff00); /* Lobby never overlays the session. */
        assert_large_pixel(module, 3400, 1900, 0x00ff00); /* Full 3072x1768 far corner. */
        assert_large_pixel(module, 850, 450, 0x00ff00); /* Separate same-PID session excluded. */
        if (strstr(mode, "controller-generic-ambiguous") == mode || !strcmp(mode, "controller-cycle") ||
            !strcmp(mode, "controller-generic-invalid-nul") ||
            !strcmp(mode, "controller-generic-cycle")) {
            assert_large_pixel(module, 650, 450, 0x00ff00); /* Ambiguous parent rejects the entire chain. */
            assert_large_pixel(module, 3400, 1800, 0x00ff00);
        } else {
            assert_large_pixel(module, 650, 450, !strcmp(mode, "controller-deep") ? 0x00ff00 : 0xffffff);
            if (!strcmp(mode, "controller-generic-640"))
                assert_large_pixel(module, 1100, 1100, 0x807f00); /* Generic popup cannot count as its own controller. */
            else
                assert_large_pixel(module, 3400, 1800, 0x807f00);
        }
        if (!strcmp(mode, "controller-compositor-exit")) {
            const char *compositor = getenv("FIXTURE_COMPOSITOR_PID");
            assert(compositor && kill((pid_t)strtol(compositor, NULL, 10), SIGTERM) == 0);
            wait_server(); wait_server();
            x.raise(display, relay); wait_server();
            assert_large_pixel(module, 3400, 1900, 0x00ff00);
            assert_large_pixel(module, 3400, 1800, 0x807f00);
        }
        if (!strcmp(mode, "controller-rate")) {
            unsigned long *composites = dlsym(render_library, "manager_test_composites");
            unsigned long before;
            struct timespec first, now, delay = {0, 2000000};
            double elapsed;
            assert(composites);
            before = *composites;
            clock_gettime(CLOCK_MONOTONIC, &first);
            do {
                assert_large_pixel(module, 3400, 1900, 0x00ff00);
                nanosleep(&delay, NULL);
                clock_gettime(CLOCK_MONOTONIC, &now);
                elapsed = now.tv_sec - first.tv_sec + (now.tv_nsec - first.tv_nsec) / 1e9;
            } while (elapsed < 0.35);
            /* Main, toolbar, nested popup each produce one composite per frame. */
            assert((*composites - before) % 3 == 0);
            assert((*composites - before) / 3 <= (unsigned long)(elapsed * 60) + 2);
            assert((*composites - before) / 3 > (unsigned long)(elapsed * 15));
        }
        {
            int (*motion)(MCDisplay *, int, int, int, unsigned long);
            int (*button)(MCDisplay *, unsigned int, int, unsigned long);
            int received = 0;
            symbol(module, "XTestFakeMotionEvent", &motion, sizeof(motion));
            symbol(module, "XTestFakeButtonEvent", &button, sizeof(button));
            assert(motion(display, -1, 3400, 1900, 0));
            assert(button(display, 1, 1, 0)); assert(button(display, 1, 0, 0));
            x.sync(display, 0);
            while (x.pending(display)) {
                Event event;
                x.event(display, &event);
                if (event.any.type == 4 && event.any.window == main_window)
                    received = 1;
            }
            assert(received);
            x.focus(display, &focused, &revert); assert(focused == main_window);
            if (!strcmp(mode, "controller-boundary")) {
                pid_t child;
                int status;
                assert(motion(display, -1, 1000, 600, 0));
                assert(button(display, 1, 1, 0)); x.sync(display, 0);
                child = fork(); assert(child >= 0);
                if (child == 0) {
                    execl("/usr/bin/xrandr", "xrandr", "--output", "screen", "--mode", "1280x720_test", (char *)NULL);
                    _exit(127);
                }
                assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
                wait_server();
                assert_large_pixel(module, 3400, 1900, 0x00ff00); /* Bounded transitional canvas still captures the app. */
                while (x.pending(display)) { Event event; x.event(display, &event); }
                assert(!motion(display, -1, 3400, 1900, 0));
                assert(!button(display, 2, 1, 0));
                assert(!button(display, 2, 0, 0));
                x.sync(display, 0);
                while (x.pending(display)) { Event event; x.event(display, &event); assert(event.any.type != 4); }
                assert(button(display, 1, 0, 0));
                x.sync(display, 0);
                MCWindow returned_root, returned_child;
                int root_x, root_y, window_x, window_y;
                unsigned int mask;
                assert(x.pointer(display, root, &returned_root, &returned_child, &root_x, &root_y,
                                 &window_x, &window_y, &mask));
                assert(!(mask & ((1U << 8) | (1U << 9))));
                assert(root_x < 1280 && root_y < 720);
            }
        }
    }
    dlclose(module);
}

int main(int argc, char **argv)
{
    void *module;
    const char *mode;
    char window_id[32];
    MCWindow modal = 0, focused;
    int revert;
    assert(argc >= 4);
    mode = argv[2];
    load_api();
    display = x.open(NULL);
    assert(display);
    root = x.root(display);
    main_pid = (unsigned long)getpid();
    if (!strcmp(mode, "foreign-worker")) {
        foreign_churn(argv[3]);
        x.close(display);
        return 0;
    }
    if (!strncmp(mode, "role-", 5) || !strncmp(mode, "controller-", 11) || !strncmp(mode, "serve-controller", 16)) {
        test_controller(argv[1], mode, argv[3]);
        puts("fixture passed");
        x.close(display);
        return 0;
    }
    frame = x.simple(display, root, 50, 50, 220, 180, 0, 0, 0x555555);
    main_window = x.simple(display, frame, 10, 10, 200, 160, 0, 0, 0x00ff00);
    relay = x.simple(display, root, 0, 0, 800, 600, 0, 0, 0x0000ff);
    owner(main_window, "gameviewer.exe", main_pid);
    owner(relay, "sdl-freerdp", main_pid);
    x.select(display, main_window, (1L << 0) | (1L << 2));
    x.select(display, relay, (1L << 0) | (1L << 2));
    x.map(display, frame); x.map(display, main_window); x.map(display, relay);
    if (!strcmp(mode, "alpha") || !strcmp(mode, "input") || !strcmp(mode, "normal-popup"))
        modal = make_modal(strcmp(mode, "normal-popup") != 0);
    x.raise(display, relay);
    x.set_focus(display, relay, 1, 0);
    wait_server();
    snprintf(window_id, sizeof(window_id), "%lu", main_window);
    if (!strcmp(mode, "serve")) {
        FILE *file = fopen(argv[3], "w");
        assert(file);
        fprintf(file, "%lu\n", main_window);
        fclose(file);
        signal(SIGTERM, terminate_fixture);
        while (!stop)
            pause();
        x.close(display);
        return 0;
    }
    setenv("UURB_MANAGER_WINDOW_ID", window_id, 1);
    setenv("UURB_MANAGER_EXPECTED_DISPLAY", getenv("DISPLAY"), 1);
    setenv("UURB_MANAGER_CAPTURE_READY_FILE", argv[3], 1);
    setenv("UURB_MANAGER_CAPTURE_NONCE", "00000000-0000-4000-8000-000000000001", 1);
    module = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!module) {
        fprintf(stderr, "%s\n", dlerror());
        return 1;
    }
    x.focus(display, &focused, &revert);
    assert(focused == relay);  /* Constructor and image polling never steal focus. */
    assert_root_pixel(module, 80, 80, 0x00ff00);
    assert_root_pixel(module, 400, 400, 0);
    x.focus(display, &focused, &revert);
    assert(focused == relay);
    if (!strcmp(mode, "foreign-churn"))
        test_foreign_churn(module, argv[0], argv[1], argv[3]);
    else if (!strcmp(mode, "trap-boundaries")) {
        void (*boundaries)(void);
        symbol(module, "manager_test_trap_boundaries", &boundaries, sizeof(boundaries));
        boundaries();
    } else if (!strcmp(mode, "owned-frame-destroy")) {
        x.destroy(display, frame); wait_server();
        assert_root_pixel(module, 80, 80, 0);
    } else if (!strcmp(mode, "getters"))
        test_getters(module);
    else if (!strcmp(mode, "alpha")) {
        assert_root_pixel(module, 110, 110, 0x00ff00);
        assert_root_pixel(module, 150, 110, 0x807f00);
        assert_root_pixel(module, 190, 110, 0xff0000);
    } else if (!strcmp(mode, "normal-popup")) {
        assert_root_pixel(module, 110, 110, 0xff0000);
        test_input(module, modal);
    } else if (!strcmp(mode, "input"))
        test_input(module, modal);
    else if (!strcmp(mode, "cleanup-input")) {
        test_cleanup_input(module);
        module = NULL;
    }
    else if (!strcmp(mode, "isolated-input")) {
        test_isolated_input(module);
        module = NULL;
    }
    else if (!strcmp(mode, "lifecycle")) {
        x.move(display, frame, 300, 100); wait_server();
        assert_root_pixel(module, 80, 80, 0);
        assert_root_pixel(module, 330, 130, 0x00ff00);
        x.resize(display, main_window, 100, 90); wait_server();
        assert_root_pixel(module, 330, 130, 0x00ff00);
        assert_root_pixel(module, 450, 130, 0);
        x.unmap(display, main_window); wait_server();
        assert_root_pixel(module, 330, 130, 0);
        x.map(display, main_window); wait_server();
        assert_root_pixel(module, 330, 130, 0x00ff00);
        x.destroy(display, main_window); wait_server();
        assert_root_pixel(module, 330, 130, 0);
    } else if (!strcmp(mode, "identity")) {
        owner(main_window, "gameviewer.exe", main_pid + 1); wait_server();
        assert_root_pixel(module, 80, 80, 0);
        owner(main_window, "gameviewer.exe", main_pid); wait_server();
        assert_root_pixel(module, 80, 80, 0x00ff00);
        owner(main_window, "unrelated", main_pid); wait_server();
        assert_root_pixel(module, 80, 80, 0);
    } else if (!strcmp(mode, "scope")) {
        MCImage *(*getter)(MCDisplay *, MCDrawable, int, int, unsigned int, unsigned int, unsigned long, int);
        MCImage *image;
        symbol(module, "XGetImage", &getter, sizeof(getter));
        image = getter(display, relay, 0, 0, 100, 100, ~0UL, 2);
        assert(pixel(image, 10, 10) == 0x0000ff);
        x.destroy_image(image);
        if (argc >= 6) {
            const char *authority = getenv("XAUTHORITY");
            char *saved_authority = authority ? strdup(authority) : NULL;
            MCDisplay *other;
            MCWindow other_root;
            setenv("XAUTHORITY", argv[5], 1);
            other = x.open(argv[4]);
            if (saved_authority) {
                setenv("XAUTHORITY", saved_authority, 1);
                free(saved_authority);
            } else
                unsetenv("XAUTHORITY");
            assert(other);
            other_root = x.root(other);
            image = getter(other, other_root, 0, 0, 100, 100, ~0UL, 2);
            assert(pixel(image, 80, 80) == 0);
            x.destroy_image(image);
            x.close(other);
        }
    }
    puts("fixture passed");
    if (module != NULL)
        dlclose(module);
    x.close(display);
    return 0;
}
