/* Preloaded into virgl_test_server by stateid.vgpu: buffers are created with GL_STATIC_DRAW instead of
 * GL_STREAM_DRAW.
 *
 * virglrenderer 1.2 creates every buffer with glBufferData(..., NULL, GL_STREAM_DRAW) and fills it with
 * glMapBufferRange. The NVIDIA driver keeps such a buffer in system memory, so each draw fetches its
 * vertices over PCIe: a 209k-vertex draw took 1.23 ms instead of 0.05 ms (glmark2 refract ran at a
 * quarter of its speed). The usage is only a hint, so rendering is unchanged; Mesa drivers ignore it.
 *
 * libepoxy looks GL functions up with eglGetProcAddress/glXGetProcAddressARB, which it finds with dlsym
 * on its own library handle, so dlsym is wrapped.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stddef.h>
#include <string.h>

#define GL_STREAM_DRAW 0x88E0
#define GL_STATIC_DRAW 0x88E4

typedef void (*proc)(void);
typedef void (*buffer_data_fn)(unsigned target, ptrdiff_t size, const void *data, unsigned usage);
typedef proc (*get_proc_fn)(const char *name);

static void *(*real_dlsym)(void *, const char *);
static buffer_data_fn real_buffer_data, real_buffer_data_arb;
static get_proc_fn real_egl_get_proc, real_glx_get_proc, real_glx_get_proc_arb;

static void buffer_data(unsigned target, ptrdiff_t size, const void *data, unsigned usage) {
    real_buffer_data(target, size, data, usage == GL_STREAM_DRAW ? GL_STATIC_DRAW : usage);
}

static void buffer_data_arb(unsigned target, ptrdiff_t size, const void *data, unsigned usage) {
    real_buffer_data_arb(target, size, data, usage == GL_STREAM_DRAW ? GL_STATIC_DRAW : usage);
}

static proc wrap(const char *name, proc p) {
    if (!p) return p;
    if (!strcmp(name, "glBufferData")) { real_buffer_data = (buffer_data_fn)p; return (proc)buffer_data; }
    if (!strcmp(name, "glBufferDataARB")) { real_buffer_data_arb = (buffer_data_fn)p; return (proc)buffer_data_arb; }
    return p;
}

static proc egl_get_proc(const char *name) { return wrap(name, real_egl_get_proc(name)); }
static proc glx_get_proc(const char *name) { return wrap(name, real_glx_get_proc(name)); }
static proc glx_get_proc_arb(const char *name) { return wrap(name, real_glx_get_proc_arb(name)); }

void *dlsym(void *handle, const char *name) {
    if (!real_dlsym) real_dlsym = dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    void *p = real_dlsym(handle, name);
    if (!p) return p;
    if (!strcmp(name, "eglGetProcAddress")) { real_egl_get_proc = (get_proc_fn)p; return (void *)egl_get_proc; }
    if (!strcmp(name, "glXGetProcAddress")) { real_glx_get_proc = (get_proc_fn)p; return (void *)glx_get_proc; }
    if (!strcmp(name, "glXGetProcAddressARB")) { real_glx_get_proc_arb = (get_proc_fn)p; return (void *)glx_get_proc_arb; }
    return (void *)wrap(name, (proc)p);
}
