/*
 * Start a bundled WebKit helper on the C library bundled in the AppImage.
 *
 * On hosts whose glibc is older than the build host's, AppRun starts the app
 * in usr/lib/bundled-libc-workdir, so WebKit exec's "././webkitgtk-6.0/<name>"
 * from there and lands on one of these. It runs the real helper through the
 * bundled loader:
 *
 *     <libc>/ld-linux-x86-64.so.2 --library-path <libc> --argv0 argv[0] <helper> ...
 *
 * A shell script did the same job until it met WebKit's bubblewrap sandbox,
 * which mounts /usr/lib and /lib64 but no /bin or /usr/bin: the script's
 * interpreter does not exist in there, and the WebProcess never starts. This
 * is built static for the same reason -- it must not need anything from the
 * host to run. Paths are relative to this executable (build-time -D values),
 * found through /proc/self/exe, which the sandbox mounts.
 *
 * Built by packaging/appimage/build-appimage.sh (step 8b) with
 *   -DREL_LIBC="../../libc" -DREL_TARGET="../../webkitgtk-6.0/<name>"
 *   -DLOADER_NAME="ld-linux-x86-64.so.2"
 */
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#if !defined(REL_LIBC) || !defined(REL_TARGET) || !defined(LOADER_NAME)
#error "REL_LIBC, REL_TARGET and LOADER_NAME must be defined"
#endif

static int join(char *out, size_t size, const char *dir, const char *a, const char *b)
{
    int n = b ? snprintf(out, size, "%s/%s/%s", dir, a, b)
              : snprintf(out, size, "%s/%s", dir, a);
    return n > 0 && (size_t)n < size;
}

int main(int argc, char **argv)
{
    char self[PATH_MAX], libdir[PATH_MAX], loader[PATH_MAX], target[PATH_MAX];
    ssize_t len = readlink("/proc/self/exe", self, sizeof self - 1);
    if (len < 0) {
        perror("sshpilot libc launcher: /proc/self/exe");
        return 127;
    }
    self[len] = '\0';
    const char *dir = dirname(self);
    if (!join(libdir, sizeof libdir, dir, REL_LIBC, NULL) ||
        !join(loader, sizeof loader, dir, REL_LIBC, LOADER_NAME) ||
        !join(target, sizeof target, dir, REL_TARGET, NULL)) {
        fprintf(stderr, "sshpilot libc launcher: path too long under %s\n", dir);
        return 127;
    }

    char **args = calloc((size_t)argc + 6, sizeof *args);
    if (!args) {
        perror("sshpilot libc launcher");
        return 127;
    }
    int i = 0;
    args[i++] = loader;
    args[i++] = "--library-path";
    args[i++] = libdir;
    args[i++] = "--argv0";
    args[i++] = argc > 0 ? argv[0] : target;
    args[i++] = target;
    for (int arg = 1; arg < argc; arg++)
        args[i++] = argv[arg];
    args[i] = NULL;

    execv(loader, args);
    fprintf(stderr, "sshpilot libc launcher: cannot run %s: ", loader);
    perror(NULL);
    return 127;
}
