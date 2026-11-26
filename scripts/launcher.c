/*
 * glance.app's main executable.
 *
 * Why this is a compiled binary and not a shell script
 * ----------------------------------------------------
 * macOS attributes Screen Recording to the code that is actually running, so
 * the running image has to live inside glance.app. Two simpler approaches
 * both fail:
 *
 *   exec .venv/bin/python     exec REPLACES the process image, so the running
 *                             binary became the system Python framework,
 *                             outside the bundle. The grant could never stick.
 *
 *   copy bin/python3.13 in    that binary is a stub: on a framework build it
 *                             immediately re-execs
 *                             Python.app/Contents/MacOS/Python so GUI code
 *                             works. Measured - the real image was already
 *                             outside the bundle before Qt was even imported,
 *                             while sys.executable still reported the bundle
 *                             path, which makes the problem easy to miss.
 *                             Copying Python.app's own binary instead is
 *                             killed outright (SIGKILL, exit 137) because its
 *                             signature does not survive the move.
 *
 * Embedding the interpreter with Py_BytesMain avoids all of it: CPython runs
 * in-process, nothing re-execs, and the image stays this binary - which is
 * signed as part of glance.app and therefore has a stable TCC identity.
 *
 * Everything the old shell launcher did (chdir, PYTHONPATH, bundle id) moves
 * here, because there is no shell any more.
 */
#include <Python.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef GLANCE_ROOT
#define GLANCE_ROOT "."
#endif
#ifndef GLANCE_SCRIPT
#define GLANCE_SCRIPT "main.py"
#endif
#ifndef GLANCE_ARGS
#define GLANCE_ARGS ""
#endif
#ifndef GLANCE_PYTHONPATH
#define GLANCE_PYTHONPATH ""
#endif
#ifndef GLANCE_BUNDLE_ID
#define GLANCE_BUNDLE_ID "com.glance.app"
#endif

#define MAX_ARGS 64

int main(int argc, char **argv) {
    (void)argc;

    /* launchd starts an agent with / as the working directory, so a relative
     * "config.yaml" would resolve to /config.yaml. */
    if (chdir(GLANCE_ROOT) != 0) {
        fprintf(stderr, "glance: cannot enter %s\n", GLANCE_ROOT);
        return 1;
    }
    setenv("PYTHONPATH", GLANCE_PYTHONPATH, 1);
    /* Output goes to a log file, where stdout would otherwise be
     * block-buffered and the session URL would never appear. */
    setenv("PYTHONUNBUFFERED", "1", 1);
    /* launchd does not export __CFBundleIdentifier the way LaunchServices
     * does, so glance would otherwise print terminal-shaped permission help. */
    setenv("GLANCE_BUNDLE_ID", GLANCE_BUNDLE_ID, 1);

    char *args[MAX_ARGS];
    int n = 0;
    args[n++] = argv[0];
    args[n++] = (char *)GLANCE_SCRIPT;

    /* Runtime argv is deliberately ignored: LaunchServices appends things
     * like -psn_0_12345 that python would reject. */
    static char buf[1024];
    strncpy(buf, GLANCE_ARGS, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';
    for (char *tok = strtok(buf, " "); tok && n < MAX_ARGS - 1;
         tok = strtok(NULL, " ")) {
        args[n++] = tok;
    }
    args[n] = NULL;

    return Py_BytesMain(n, args);
}
