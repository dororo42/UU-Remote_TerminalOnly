import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SspiInitializationTests(unittest.TestCase):
    def run_probe(self, fail_first):
        source = Path(os.environ.get("UURB_SSPI_TEST_SOURCE", ROOT / "src/winpr_sspi_shim.c")).read_text()
        declarations = source[source.index("static SecurityFunctionTableA patched_a;"):source.index("#define SSPI_PACKAGE_NEGOTIATE_ID")]
        start = source.find("static BOOL CALLBACK initialize_interface_a")
        if start < 0:
            start = source.index("__declspec(dllexport) PSecurityFunctionTableA")
        functions = source[start:]
        harness = r'''
#define _POSIX_C_SOURCE 200809L
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define WINAPI
#define CALLBACK
#define __declspec(x)
#define TRUE 1
#define FALSE 0
typedef int BOOL;
typedef void *PVOID;
typedef struct { pthread_mutex_t mutex; int complete; } INIT_ONCE, *PINIT_ONCE;
#define INIT_ONCE_STATIC_INIT {PTHREAD_MUTEX_INITIALIZER, 0}
typedef void (*fn)(void);
typedef struct { int marker; fn AcquireCredentialsHandleA; fn InitializeSecurityContextA; } SecurityFunctionTableA, *PSecurityFunctionTableA;
typedef struct { int marker; fn AcquireCredentialsHandleW; fn InitializeSecurityContextW; } SecurityFunctionTableW, *PSecurityFunctionTableW;
static void patched_acquire_credentials_a(void) {}
static void patched_acquire_credentials_w(void) {}
static void patched_initialize_context_a(void) {}
static void patched_initialize_context_w(void) {}
static _Atomic int calls_a, calls_w, failures;
static int fail_first;
static pthread_barrier_t start_barrier;
static SecurityFunctionTableA real_a = {123, NULL, NULL};
static SecurityFunctionTableW real_w = {456, NULL, NULL};
static void *init_winpr_interface(const char *name) {
    int is_a = name[strlen(name) - 1] == 'A';
    int previous = atomic_fetch_add(is_a ? &calls_a : &calls_w, 1);
    struct timespec delay = {0, 30000000};
    nanosleep(&delay, NULL);
    if (fail_first && previous == 0) return NULL;
    return is_a ? (void *)&real_a : (void *)&real_w;
}
BOOL InitOnceExecuteOnce(PINIT_ONCE once, BOOL (*callback)(PINIT_ONCE, PVOID, PVOID *), PVOID parameter, PVOID *context) {
    pthread_mutex_lock(&once->mutex);
    if (!once->complete) once->complete = callback(once, parameter, context);
    int result = once->complete;
    pthread_mutex_unlock(&once->mutex);
    return result;
}
'''
        tail = r'''
static void *reader(void *unused) {
    (void)unused;
    pthread_barrier_wait(&start_barrier);
    for (int i = 0; i < 100; ++i) {
        SecurityFunctionTableA *a = InitSecurityInterfaceA();
        SecurityFunctionTableW *w = InitSecurityInterfaceW();
        if (!a || !w || a->marker != 123 || w->marker != 456 ||
            a->AcquireCredentialsHandleA != patched_acquire_credentials_a ||
            a->InitializeSecurityContextA != patched_initialize_context_a ||
            w->AcquireCredentialsHandleW != patched_acquire_credentials_w ||
            w->InitializeSecurityContextW != patched_initialize_context_w)
            atomic_fetch_add(&failures, 1);
    }
    return NULL;
}
int main(int argc, char **argv) {
    (void)argc;
    fail_first = atoi(argv[1]);
    if (fail_first && (InitSecurityInterfaceA() != NULL || InitSecurityInterfaceW() != NULL))
        return 2;
    pthread_t threads[16];
    pthread_barrier_init(&start_barrier, NULL, 17);
    for (int i = 0; i < 16; ++i) pthread_create(&threads[i], NULL, reader, NULL);
    pthread_barrier_wait(&start_barrier);
    for (int i = 0; i < 16; ++i) pthread_join(threads[i], NULL);
    printf("calls_a=%d calls_w=%d invalid_tables=%d\n", calls_a, calls_w, failures);
    return failures != 0 || calls_a != 1 + fail_first || calls_w != 1 + fail_first;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "probe.c").write_text(harness + declarations + functions + tail)
            subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread", str(path / "probe.c"), "-o", str(path / "probe")], check=True, capture_output=True, text=True)
            result = subprocess.run([str(path / "probe"), str(int(fail_first))], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_concurrent_callers_obtain_one_complete_table_per_interface(self):
        self.run_probe(False)

    def test_failed_initialization_can_be_retried_without_partial_tables(self):
        self.run_probe(True)


if __name__ == "__main__":
    unittest.main()
