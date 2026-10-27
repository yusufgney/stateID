/* Preloaded into the programs of a freezable session (by the helper, from its root-owned directory):
 * /dev/udmabuf cannot be opened there, as if the user had no access to it. Mesa's software rendering
 * (llvmpipe for OpenGL, lavapipe for Vulkan) opens it when it starts and works without it, but an open
 * /dev/udmabuf keeps CRIU from freezing the session. Built by install-helper.sh. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>

typedef int (*open_fn)(const char *, int, ...);
typedef int (*openat_fn)(int, const char *, int, ...);

static int hidden(const char *path) {
    return path && strcmp(path, "/dev/udmabuf") == 0;
}

#define MODE(flags) \
    mode_t mode = 0; \
    if ((flags) & (O_CREAT | O_TMPFILE)) { \
        va_list ap; \
        va_start(ap, flags); \
        mode = va_arg(ap, mode_t); \
        va_end(ap); \
    }

#define OPEN(name) \
    int name(const char *path, int flags, ...) { \
        static open_fn real; \
        MODE(flags) \
        if (hidden(path)) { errno = EACCES; return -1; } \
        if (!real) real = (open_fn)dlsym(RTLD_NEXT, #name); \
        return real(path, flags, mode); \
    }

#define OPENAT(name) \
    int name(int dir, const char *path, int flags, ...) { \
        static openat_fn real; \
        MODE(flags) \
        if (hidden(path)) { errno = EACCES; return -1; } \
        if (!real) real = (openat_fn)dlsym(RTLD_NEXT, #name); \
        return real(dir, path, flags, mode); \
    }

OPEN(open)
OPEN(open64)
OPENAT(openat)
OPENAT(openat64)

/* what open() becomes with _FORTIFY_SOURCE */
#define OPEN_2(name) \
    int name(const char *path, int flags) { \
        static int (*real)(const char *, int); \
        if (hidden(path)) { errno = EACCES; return -1; } \
        if (!real) real = (int (*)(const char *, int))dlsym(RTLD_NEXT, #name); \
        return real(path, flags); \
    }

#define OPENAT_2(name) \
    int name(int dir, const char *path, int flags) { \
        static int (*real)(int, const char *, int); \
        if (hidden(path)) { errno = EACCES; return -1; } \
        if (!real) real = (int (*)(int, const char *, int))dlsym(RTLD_NEXT, #name); \
        return real(dir, path, flags); \
    }

OPEN_2(__open_2)
OPEN_2(__open64_2)
OPENAT_2(__openat_2)
OPENAT_2(__openat64_2)
