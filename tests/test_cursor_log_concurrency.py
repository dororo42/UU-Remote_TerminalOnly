"""Exercise cursor logging against Wine's EOF-then-pwrite append behavior."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


PROBE = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
typedef int HANDLE;
typedef uint32_t DWORD;
typedef pthread_mutex_t SRWLOCK;
#define SRWLOCK_INIT PTHREAD_MUTEX_INITIALIZER
#define INVALID_HANDLE_VALUE (-1)
static HANDLE log_file;
static _Thread_local DWORD last_error;
static int force_collision, fail_write;
static pthread_barrier_t after_eof;
static pthread_mutex_t order = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t completed = PTHREAD_COND_INITIALIZER;
static int active_written, writes_in_flight, maximum_in_flight, write_calls;
static DWORD GetLastError(void) { return last_error; }
static void SetLastError(DWORD value) { last_error = value; }
static void AcquireSRWLockExclusive(SRWLOCK *lock)
{ assert(pthread_mutex_lock(lock) == 0); SetLastError(101); }
static void ReleaseSRWLockExclusive(SRWLOCK *lock)
{ assert(pthread_mutex_unlock(lock) == 0); SetLastError(102); }
static int WriteFile(HANDLE fd, const char *data, DWORD length,
                     DWORD *written, void *unused)
{
    struct stat status;
    (void)unused;
    assert(fstat(fd, &status) == 0);
    pthread_mutex_lock(&order);
    write_calls++;
    writes_in_flight++;
    if (writes_in_flight > maximum_in_flight)
        maximum_in_flight = writes_in_flight;
    pthread_mutex_unlock(&order);
    if (force_collision) {
        pthread_barrier_wait(&after_eof);
        pthread_mutex_lock(&order);
        if (length == 42)
            while (!active_written) pthread_cond_wait(&completed, &order);
    } else {
        /* Widen Wine's non-atomic fstat/pwrite interval. */
        sched_yield();
        pthread_mutex_lock(&order);
    }
    if (!fail_write) {
        assert(pwrite(fd, data, length, status.st_size) == (ssize_t)length);
        *written = length;
    }
    if (force_collision && length == 45) {
        active_written = 1;
        pthread_cond_signal(&completed);
    }
    writes_in_flight--;
    pthread_mutex_unlock(&order);
    SetLastError(fail_write ? 5 : 103);
    return !fail_write;
}
'''

MAIN = r'''
static const char active[] = "UU relay cursor guard active (cursor 24x24)\r\n";
static const char hidden[] = "UU cursor guard replaced a hidden cursor\r\n";
static void *writer(void *message)
{
    for (int index = 0; index < (force_collision ? 1 : 1000); index++) {
        SetLastError(701 + (DWORD)((const char *)message == active));
        if (force_collision) {
            DWORD count;
            WriteFile(log_file, message, (DWORD)strlen(message), &count, NULL);
        } else {
            write_log(message);
            assert(GetLastError() == 701 + (DWORD)((const char *)message == active));
        }
    }
    return NULL;
}
int main(int argc, char **argv)
{
    assert(argc == 3);
    log_file = open(argv[2], O_CREAT | O_TRUNC | O_RDWR, 0600);
    assert(log_file >= 0);
    if (!strcmp(argv[1], "invalid")) {
        int fd = log_file;
        log_file = INVALID_HANDLE_VALUE;
        SetLastError(777);
        write_log(active);
        assert(GetLastError() == 777 && write_calls == 0);
        /* An invalid handle must not leave the lock held. */
        log_file = fd;
        write_log(active);
        assert(GetLastError() == 777 && write_calls == 1);
    } else if (!strcmp(argv[1], "failure")) {
        fail_write = 1;
        SetLastError(778);
        write_log(active);
        assert(GetLastError() == 778 && write_calls == 1);
        fail_write = 0;
        write_log(hidden);
        assert(GetLastError() == 778 && write_calls == 2);
    } else {
        pthread_t first, second;
        force_collision = !strcmp(argv[1], "negative-control");
        assert(pthread_barrier_init(&after_eof, NULL, 2) == 0);
        assert(pthread_create(&first, NULL, writer, (void *)active) == 0);
        assert(pthread_create(&second, NULL, writer, (void *)hidden) == 0);
        assert(pthread_join(first, NULL) == 0);
        assert(pthread_join(second, NULL) == 0);
        struct stat status;
        assert(fstat(log_file, &status) == 0);
        if (force_collision) {
            char bytes[45];
            assert(pread(log_file, bytes, sizeof(bytes), 0) == 45);
            assert(!memcmp(bytes, hidden, 42));
            assert(!memcmp(bytes + 42, active + 42, 3));
            assert(maximum_in_flight == 2 && status.st_size == 45);
        } else {
            assert(maximum_in_flight == 1 && write_calls == 2000);
            assert(status.st_size == 1000 * (45 + 42));
        }
        pthread_barrier_destroy(&after_eof);
    }
    assert(close(log_file) == 0);
    return 0;
}
'''


class CursorLogConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        source = (ROOT / "src/uu_cursor_guard.c").read_text()
        lock = re.search(r"^static SRWLOCK log_lock = .*?;", source, re.M)
        function = re.search(r"^static void write_log\(.*?^}", source, re.M | re.S)
        assert lock and function
        path = Path(cls.temporary.name) / "probe.c"
        path.write_text(PROBE + lock.group() + "\n" + function.group() + MAIN)
        cls.probe = path.with_suffix("")
        subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-pthread", str(path), "-o", str(cls.probe)],
                       capture_output=True, text=True, check=True)

    def run_case(self, case):
        output = Path(self.temporary.name) / (case + ".log")
        result = subprocess.run([str(self.probe), case, str(output)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_bytes()

    def test_concurrent_readiness_and_callback_records_remain_complete(self):
        data = self.run_case("serialized")
        records = data.splitlines(keepends=True)
        self.assertEqual(records.count(b"UU relay cursor guard active (cursor 24x24)\r\n"), 1000)
        self.assertEqual(records.count(b"UU cursor guard replaced a hidden cursor\r\n"), 1000)
        self.assertEqual(len(records), 2000)

    def test_original_append_interleaving_reproduces_missing_readiness(self):
        data = self.run_case("negative-control")
        self.assertEqual(data, b"UU cursor guard replaced a hidden cursor\r\n)\r\n")

    def test_invalid_handle_preserves_last_error_and_releases_lock(self):
        self.assertEqual(self.run_case("invalid"), b"UU relay cursor guard active (cursor 24x24)\r\n")

    def test_write_failure_preserves_last_error_and_allows_next_write(self):
        self.assertEqual(self.run_case("failure"), b"UU cursor guard replaced a hidden cursor\r\n")


if __name__ == "__main__":
    unittest.main()
