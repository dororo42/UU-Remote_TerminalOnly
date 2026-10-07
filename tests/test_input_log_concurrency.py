"""Exercise the extracted input logger against Wine's EOF/pwrite append race."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
READINESS = (
    b"UU SendInput bridge active\r\n",
    b"UU Wine event-log compatibility active\r\n",
    b"UU native host shell actions active\r\n",
)
CALLBACK = b"call=1 category=mouse\r\n"

PROBE = r'''
#define _GNU_SOURCE
#include <assert.h>
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
static int force_collision, fail_write, readiness_written;
static int writes_in_flight, maximum_in_flight, write_calls;
static pthread_barrier_t after_eof;
static pthread_mutex_t order = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t completed = PTHREAD_COND_INITIALIZER;
static const char *records[] = {
 "UU SendInput bridge active\r\n",
 "UU Wine event-log compatibility active\r\n",
 "UU native host shell actions active\r\n",
 "call=1 category=mouse\r\n"
};
static DWORD GetLastError(void) { return last_error; }
static void SetLastError(DWORD value) { last_error = value; }
#ifndef UNSYNCHRONIZED_CONTROL
static void AcquireSRWLockExclusive(SRWLOCK *lock)
{ assert(pthread_mutex_lock(lock) == 0); SetLastError(101); }
static void ReleaseSRWLockExclusive(SRWLOCK *lock)
{ assert(pthread_mutex_unlock(lock) == 0); SetLastError(102); }
#endif
static int WriteFile(HANDLE fd, const char *data, DWORD length,
                     DWORD *written, void *unused)
{
 struct stat status;
 (void)unused;
 assert(fstat(fd, &status) == 0);
 pthread_mutex_lock(&order);
 write_calls++;
 if (++writes_in_flight > maximum_in_flight) maximum_in_flight=writes_in_flight;
 pthread_mutex_unlock(&order);
 if (force_collision) {
  int barrier_result=pthread_barrier_wait(&after_eof);
  assert(barrier_result==0 || barrier_result==PTHREAD_BARRIER_SERIAL_THREAD);
  pthread_mutex_lock(&order);
  if (data == records[3])
   while (!readiness_written) pthread_cond_wait(&completed,&order);
 } else {
  sched_yield();
  pthread_mutex_lock(&order);
 }
 if (!fail_write) {
  assert(pwrite(fd,data,length,status.st_size) == (ssize_t)length);
  *written=length;
 }
 if (force_collision && data == records[0]) {
  readiness_written=1;
  pthread_cond_signal(&completed);
 }
 writes_in_flight--;
 pthread_mutex_unlock(&order);
 SetLastError(fail_write ? 5 : 103);
 return !fail_write;
}
'''

MAIN = r'''
static void *writer(void *arg)
{
 size_t index=(size_t)(uintptr_t)arg;
 for (int round=0;round < (force_collision ? 1 : 1000);round++) {
  SetLastError(701+(DWORD)index);
  write_log(records[index]);
  assert(GetLastError()==701+(DWORD)index);
 }
 return NULL;
}
int main(int argc,char **argv)
{
 assert(argc==3);
 log_file=open(argv[2],O_CREAT|O_TRUNC|O_RDWR,0600);
 assert(log_file>=0);
 if (!strcmp(argv[1],"invalid")) {
  int fd=log_file;log_file=INVALID_HANDLE_VALUE;
  SetLastError(777);write_log(records[0]);
  assert(GetLastError()==777 && write_calls==0);
  log_file=fd;write_log(records[0]);
  assert(GetLastError()==777 && write_calls==1);
 } else if (!strcmp(argv[1],"failure")) {
  fail_write=1;SetLastError(778);write_log(records[0]);
  assert(GetLastError()==778 && write_calls==1);
  fail_write=0;write_log(records[3]);
  assert(GetLastError()==778 && write_calls==2);
 } else {
  pthread_t threads[4];
  force_collision=!strcmp(argv[1],"negative-control");
  assert(pthread_barrier_init(&after_eof,NULL,2)==0);
  size_t count=force_collision ? 2 : 4;
  for (size_t i=0;i<count;i++)
   assert(pthread_create(&threads[i],NULL,writer,(void *)(uintptr_t)(force_collision && i==1 ? 3 : i))==0);
  for (size_t i=0;i<count;i++) assert(pthread_join(threads[i],NULL)==0);
  struct stat status;assert(fstat(log_file,&status)==0);
  if (force_collision) {
   assert(maximum_in_flight==2 && write_calls==2);
   assert(status.st_size==(off_t)strlen(records[0]));
  } else {
   size_t total=0;for(size_t i=0;i<4;i++)total+=strlen(records[i]);
   assert(maximum_in_flight==1 && write_calls==4000);
   assert(status.st_size==(off_t)(1000*total));
  }
  assert(pthread_barrier_destroy(&after_eof)==0);
 }
 assert(close(log_file)==0);return 0;
}
'''


class InputLogConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        source = (ROOT / "src/uu_input_bridge.c").read_text()
        lock = re.search(r"^static SRWLOCK log_lock = .*?;", source, re.M)
        function = re.search(r"^static void write_log\(.*?^}", source, re.M | re.S)
        assert lock and function
        cls.probes = {}
        for mode in ("serialized", "negative-control"):
            helper = function.group()
            preamble = PROBE
            declaration = lock.group()
            if mode == "negative-control":
                # Keep the exact extracted helper, removing only its lock pair.
                # This isolates the old append behavior from error preservation.
                helper = re.sub(r"^\s*(?:Acquire|Release)SRWLockExclusive\(&log_lock\);\n",
                                "", helper, flags=re.M)
                preamble = "#define UNSYNCHRONIZED_CONTROL\n" + PROBE
                declaration = ""
            path = Path(cls.temporary.name) / (mode + ".c")
            path.write_text(preamble + declaration + "\n" + helper + MAIN)
            cls.probes[mode] = path.with_suffix("")
            subprocess.run(["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-pthread", str(path), "-o", str(cls.probes[mode])],
                           capture_output=True, text=True, check=True)

    def run_case(self, case):
        output = Path(self.temporary.name) / (case + ".log")
        probe = self.probes["negative-control" if case == "negative-control" else "serialized"]
        result = subprocess.run([str(probe), case, str(output)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.read_bytes()

    def test_all_readiness_lines_and_hook_records_remain_complete(self):
        records = self.run_case("serialized").splitlines(keepends=True)
        for expected in (*READINESS, CALLBACK):
            self.assertEqual(records.count(expected), 1000)
        self.assertEqual(len(records), 4000)

    def test_unsynchronized_extracted_helper_loses_readiness_deterministically(self):
        self.assertEqual(self.run_case("negative-control"),
                         CALLBACK + READINESS[0][len(CALLBACK):])

    def test_invalid_handle_preserves_last_error_and_releases_lock(self):
        self.assertEqual(self.run_case("invalid"), READINESS[0])

    def test_write_failure_preserves_last_error_and_allows_next_write(self):
        self.assertEqual(self.run_case("failure"), CALLBACK)


if __name__ == "__main__":
    unittest.main()
