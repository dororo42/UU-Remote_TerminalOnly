#define _GNU_SOURCE
#include "../../src/x11_manager_capture_api.h"
#include <assert.h>
#include <dlfcn.h>
#include <string.h>

/* Test-only counters around the real Render server, never a fake pixel result. */
unsigned long manager_test_composites;
static void *real_library;
static void resolve(const char *name, void *destination, size_t size)
{
    void *symbol;
    if (real_library == NULL)
        real_library = dlopen("/usr/lib/x86_64-linux-gnu/libXrender.so.1", RTLD_NOW | RTLD_LOCAL);
    assert(real_library != NULL);
    symbol = dlsym(real_library, name);
    assert(symbol != NULL && size == sizeof(symbol));
    memcpy(destination, &symbol, size);
}

MCRenderFormat *XRenderFindVisualFormat(MCDisplay *display, MCVisual *visual)
{
    MCRenderFormat *(*real)(MCDisplay *, MCVisual *);
    resolve("XRenderFindVisualFormat", &real, sizeof(real));
    return real(display, visual);
}

MCPicture XRenderCreatePicture(MCDisplay *display, MCDrawable drawable,
                               const MCRenderFormat *format, unsigned long mask, const void *attributes)
{
    MCPicture (*real)(MCDisplay *, MCDrawable, const MCRenderFormat *, unsigned long, const void *);
    resolve("XRenderCreatePicture", &real, sizeof(real));
    return real(display, drawable, format, mask, attributes);
}

void XRenderFreePicture(MCDisplay *display, MCPicture picture)
{
    void (*real)(MCDisplay *, MCPicture);
    resolve("XRenderFreePicture", &real, sizeof(real));
    real(display, picture);
}

void XRenderFillRectangle(MCDisplay *display, int operation, MCPicture picture,
                          const MCRenderColor *color, int x, int y, unsigned int width, unsigned int height)
{
    void (*real)(MCDisplay *, int, MCPicture, const MCRenderColor *, int, int, unsigned int, unsigned int);
    resolve("XRenderFillRectangle", &real, sizeof(real));
    real(display, operation, picture, color, x, y, width, height);
}

void XRenderComposite(MCDisplay *display, int operation, MCPicture source, MCPicture mask,
                      MCPicture destination, int sx, int sy, int mx, int my, int dx, int dy,
                      unsigned int width, unsigned int height)
{
    void (*real)(MCDisplay *, int, MCPicture, MCPicture, MCPicture,
                 int, int, int, int, int, int, unsigned int, unsigned int);
    resolve("XRenderComposite", &real, sizeof(real));
    ++manager_test_composites;
    real(display, operation, source, mask, destination, sx, sy, mx, my, dx, dy, width, height);
}
