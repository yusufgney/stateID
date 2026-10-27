/* Preloaded into virgl_test_server by stateid.vgpu. It does two things:
 *
 * 1. Buffers are created with GL_STATIC_DRAW instead of GL_STREAM_DRAW.
 *
 * virglrenderer 1.2 creates every buffer with glBufferData(..., NULL, GL_STREAM_DRAW) and fills it with
 * glMapBufferRange. The NVIDIA driver keeps such a buffer in system memory, so each draw fetches its
 * vertices over PCIe: a 209k-vertex draw took 1.23 ms instead of 0.05 ms (glmark2 refract ran at a
 * quarter of its speed). The usage is only a hint, so rendering is unchanged; Mesa drivers ignore it.
 *
 * libepoxy looks GL functions up with eglGetProcAddress/glXGetProcAddressARB, which it finds with dlsym
 * on its own library handle, so dlsym is wrapped.
 *
 * 2. When the keeper rebuilds a program's GPU state on a new server, a resource must use the shared
 * memory the program already maps. vtest creates it with syscall(SYS_memfd_create, "vtest-res-<id>");
 * this asks the keeper ($STATEID_VGPU_SHM: 4 bytes resource id -> 1 byte, with an fd if it has one)
 * and returns its fd instead. Otherwise, and without the keeper, a new memfd is made as usual.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <unistd.h>

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

/* ------------------------------------------------------------------ shared memory from the keeper */
static int keeper = -1;
static pid_t keeper_pid;  /* the server forks per client: each process needs its own connection */

static int keeper_shm(const char *name) {
    const char *path = getenv("STATEID_VGPU_SHM");
    if (!path || !name || strncmp(name, "vtest-res-", 10)) return -1;
    if (keeper < 0 || keeper_pid != getpid()) {
        struct sockaddr_un addr = {.sun_family = AF_UNIX};
        if (strlen(path) >= sizeof(addr.sun_path)) return -1;
        strcpy(addr.sun_path, path);
        keeper = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        keeper_pid = getpid();
        if (keeper < 0 || connect(keeper, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
            if (keeper >= 0) close(keeper);
            keeper = -1;
            return -1;
        }
    }
    uint32_t id = (uint32_t)strtoul(name + 10, NULL, 10);
    if (write(keeper, &id, 4) != 4) return -1;
    char answer;
    char control[CMSG_SPACE(sizeof(int))];
    struct iovec iov = {&answer, 1};
    struct msghdr msg = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = control, .msg_controllen = sizeof(control)};
    if (recvmsg(keeper, &msg, MSG_CMSG_CLOEXEC) != 1 || answer != 'y') return -1;
    struct cmsghdr *c = CMSG_FIRSTHDR(&msg);
    if (!c || c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS) return -1;
    int fd;
    memcpy(&fd, CMSG_DATA(c), sizeof(fd));
    return fd;
}

long syscall(long number, ...) {
    va_list ap;
    va_start(ap, number);
    long a[6];
    for (int i = 0; i < 6; i++) a[i] = va_arg(ap, long);
    va_end(ap);
    if (number == SYS_memfd_create) {
        int fd = keeper_shm((const char *)a[0]);
        if (fd >= 0) return fd;
    }
    static long (*real_syscall)(long, ...);
    if (!real_syscall) real_syscall = (long (*)(long, ...))dlsym(RTLD_NEXT, "syscall");
    return real_syscall(number, a[0], a[1], a[2], a[3], a[4], a[5]);
}
