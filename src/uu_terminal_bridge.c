/* Modified from GaryOAO/UUWay commit f192f65 (AGPL-3.0).
 * Source: https://github.com/GaryOAO/UUWay (see LICENSE.UUWay at repo root).
 * Changes: out-of-range resize frames are ignored instead of dropping the
 * viewer; a live viewer outlives a dropped anchor; viewer-send and idle
 * timeouts are separate and tunable; anchor reads tolerate EAGAIN;
 * session-limit rejections are logged. Full history in this repository. */
#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/prctl.h>
#include <poll.h>
#include <pty.h>
#include <pwd.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <stddef.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "terminal_bridge_protocol.h"

/* Connection handlers plus persistent session holders. */
#define MAX_SESSIONS 32
#define MAX_ANCHORS 4
#define HANDSHAKE_TIMEOUT_MS 5000
#define FRAME_TIMEOUT_MS 5000
#define IO_TIMEOUT_MS 5000
#define VIEWER_SEND_TIMEOUT_MS 30000
#define IDLE_GRACE_MS 60000
#define POLL_SLICE_MS 100
#define REDRAW_DELAY_MS 800

static volatile sig_atomic_t stop_requested;
static volatile sig_atomic_t children_changed;
static pid_t handlers[MAX_SESSIONS];
static pid_t broker_pid;
static char ready_path[4096];
static struct stat ready_identity;
static int ready_identity_valid;
/* Overridable through UURB_IO_TIMEOUT_MS / UURB_VIEWER_SEND_TIMEOUT_MS /
 * UURB_IDLE_GRACE_MS for tests and deployments with unusual link budgets. */
static int64_t io_timeout_ms = IO_TIMEOUT_MS;
static int64_t viewer_send_timeout_ms = VIEWER_SEND_TIMEOUT_MS;
static int64_t idle_grace_ms = IDLE_GRACE_MS;

static void handle_signal(int signal_number)
{
    if (signal_number == SIGCHLD)
        children_changed = 1;
    else
        stop_requested = 1;
}

static int constant_time_equal(const char *left, const char *right, size_t size)
{
    unsigned char difference = 0;
    size_t index;

    for (index = 0; index < size; index++)
        difference |= (unsigned char)left[index] ^ (unsigned char)right[index];
    return difference == 0;
}

static int token_is_valid(const char *token)
{
    size_t index;

    if (token == NULL || strlen(token) != UURB_TERMINAL_TOKEN_LENGTH)
        return 0;
    for (index = 0; index < UURB_TERMINAL_TOKEN_LENGTH; index++) {
        if (!((token[index] >= '0' && token[index] <= '9') ||
              (token[index] >= 'a' && token[index] <= 'f')))
            return 0;
    }
    return 1;
}

static int64_t monotonic_milliseconds(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return 0;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int wait_for_fd(int fd, short events, int64_t deadline)
{
    struct pollfd descriptor = {.fd = fd, .events = events};

    while (!stop_requested) {
        int64_t remaining = deadline - monotonic_milliseconds();
        int timeout;
        int result;

        if (remaining <= 0)
            return 0;
        timeout = remaining > POLL_SLICE_MS ? POLL_SLICE_MS : (int)remaining;
        result = poll(&descriptor, 1, timeout);
        if (result > 0)
            return descriptor.revents & (events | POLLERR | POLLHUP | POLLNVAL)
                       ? 1 : 0;
        if (result < 0 && errno != EINTR)
            return 0;
    }
    return 0;
}

static int read_exact_deadline(int fd, void *buffer, size_t size, int64_t deadline)
{
    unsigned char *cursor = buffer;

    while (size > 0) {
        ssize_t received;

        if (!wait_for_fd(fd, POLLIN, deadline))
            return stop_requested ? -2 : -1;
        received = recv(fd, cursor, size, MSG_DONTWAIT);
        if (received == 0)
            return 0;
        if (received < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            return -1;
        }
        cursor += received;
        size -= (size_t)received;
    }
    return 1;
}

static int write_all_fd(int fd, const void *buffer, size_t size)
{
    const unsigned char *cursor = buffer;
    const int64_t deadline = monotonic_milliseconds() + io_timeout_ms;

    while (size > 0) {
        ssize_t written;

        if (!wait_for_fd(fd, POLLOUT, deadline))
            return 0;
        written = write(fd, cursor, size);

        if (written <= 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            return 0;
        }
        cursor += written;
        size -= (size_t)written;
    }
    return 1;
}

static int send_all(int fd, const void *buffer, size_t size)
{
    const unsigned char *cursor = buffer;
    const int64_t deadline = monotonic_milliseconds() + io_timeout_ms;

    while (size > 0) {
        ssize_t sent;

        if (!wait_for_fd(fd, POLLOUT, deadline))
            return 0;
        sent = send(fd, cursor, size, MSG_NOSIGNAL | MSG_DONTWAIT);

        if (sent <= 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            return 0;
        }
        cursor += sent;
        size -= (size_t)sent;
    }
    return 1;
}

/* Output toward a viewer: a stalled controller window must not cost the
 * session — dropping the backlog is cheaper than dropping the shell. */
static int send_all_viewer(int fd, const void *buffer, size_t size)
{
    const unsigned char *cursor = buffer;
    const int64_t deadline = monotonic_milliseconds() + viewer_send_timeout_ms;

    while (size > 0) {
        ssize_t sent;

        if (!wait_for_fd(fd, POLLOUT, deadline))
            return 0;
        sent = send(fd, cursor, size, MSG_NOSIGNAL | MSG_DONTWAIT);

        if (sent <= 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
                continue;
            return 0;
        }
        cursor += sent;
        size -= (size_t)sent;
    }
    return 1;
}

static void close_fd(int *fd);

/* Half-close toward the viewer so a clean EOF reaches it before the RST. */
static void close_viewer(int *fd)
{
    if (*fd >= 0)
        shutdown(*fd, SHUT_WR);
    close_fd(fd);
}

static void stop_shell(pid_t child)
{
    int status;
    unsigned int attempt;

    if (child <= 0)
        return;
    if (waitpid(child, &status, WNOHANG) == child)
        return;
    kill(-child, SIGHUP);
    for (attempt = 0; attempt < 20; attempt++) {
        if (waitpid(child, &status, WNOHANG) == child)
            return;
        usleep(50000);
    }
    kill(-child, SIGTERM);
    for (attempt = 0; attempt < 20; attempt++) {
        if (waitpid(child, &status, WNOHANG) == child)
            return;
        usleep(50000);
    }
    kill(-child, SIGKILL);
    while (waitpid(child, &status, 0) < 0 && errno == EINTR)
        ;
}

static void run_login_shell(void)
{
    struct passwd *account = getpwuid(getuid());
    const char *home;
    const char *shell;

    if (account == NULL)
        _exit(126);
    home = account->pw_dir != NULL ? account->pw_dir : "/";
    shell = account->pw_shell != NULL && account->pw_shell[0] != '\0'
                ? account->pw_shell
                : "/bin/bash";
    unsetenv("UURB_TERMINAL_BRIDGE_TOKEN");
    unsetenv("UURB_TERMINAL_BRIDGE_PORT");
    setenv("HOME", home, 1);
    setenv("USER", account->pw_name, 1);
    setenv("LOGNAME", account->pw_name, 1);
    setenv("SHELL", shell, 1);
    setenv("TERM", "xterm-256color", 1);
    if (chdir(home) != 0)
        _exit(126);
    execl(shell, shell, "-l", (char *)NULL);
    _exit(127);
}

static int apply_resize(int pty_master, const unsigned char *payload)
{
    struct winsize size;
    uint16_t columns_network;
    uint16_t rows_network;

    memcpy(&columns_network, payload, sizeof(columns_network));
    memcpy(&rows_network, payload + sizeof(columns_network),
           sizeof(rows_network));
    memset(&size, 0, sizeof(size));
    size.ws_col = ntohs(columns_network);
    size.ws_row = ntohs(rows_network);
    if (size.ws_col == 0 || size.ws_col > 4096 ||
        size.ws_row == 0 || size.ws_row > 4096)
        return 0;
    {
        struct winsize current;
        pid_t group;

        /* An unchanged size sends no SIGWINCH; a returning viewer still
         * needs the foreground program to redraw. */
        if (ioctl(pty_master, TIOCGWINSZ, &current) == 0 &&
            current.ws_col == size.ws_col && current.ws_row == size.ws_row) {
            group = tcgetpgrp(pty_master);
            if (group > 0)
                kill(-group, SIGWINCH);
            return 1;
        }
    }
    return ioctl(pty_master, TIOCSWINSZ, &size) == 0;
}

struct handshake {
    struct winsize size;
    uint16_t version;
    uint8_t role;
    char name[UURB_TERMINAL_MAX_SESSION_NAME + 1];
};

/* Session names come from UU's psmux sessions ("session3"). */
static int session_name_is_valid(const char *name, size_t length)
{
    size_t index;

    if (length == 0 || length > UURB_TERMINAL_MAX_SESSION_NAME ||
        name[0] == '.' || name[0] == '-')
        return 0;
    for (index = 0; index < length; index++) {
        char c = name[index];

        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.'))
            return 0;
    }
    return 1;
}

static int active_sessions(void);
static void send_busy_byte(int fd);

/* Strip OSC 3008 "hierarchical context signalling" sequences (systemd 257+
 * installs a profile hook on Ubuntu 26.04 that emits them around every
 * command). UU's terminal renders them as literal text. The filter is
 * stateful: sequences split across reads are handled. */
static const char OSC3008_INTRO[] = "\x1b]3008;";

static struct {
    int in_sequence;        /* inside an OSC 3008 payload to strip */
    size_t intro_matched;   /* prefix of OSC3008_INTRO matched so far */
    int esc_seen;           /* inside the sequence: ESC of a possible ST */
} osc3008_filter;

static void osc3008_reset(void)
{
    osc3008_filter.in_sequence = 0;
    osc3008_filter.intro_matched = 0;
    osc3008_filter.esc_seen = 0;
}

/* Feed one chunk through the filter; returns the filtered length. */
static size_t osc3008_filter_chunk(const unsigned char *in, size_t len,
                                   unsigned char *out)
{
    size_t in_index = 0, out_index = 0;

    while (in_index < len) {
        unsigned char c = in[in_index++];
        int emit = 1;

        if (osc3008_filter.in_sequence) {
            if (osc3008_filter.esc_seen) {
                osc3008_reset();
                if (c == 0x1b) {
                    emit = 0;
                    osc3008_filter.intro_matched = 1;   /* a new escape begins */
                } else if (c == '\\') {
                    emit = 0;                           /* ST completed, dropped */
                } else {
                    /* Spec-impossible: treat as resumed content. */
                }
            } else if (c == 0x07) {
                osc3008_reset();                        /* BEL terminator */
                emit = 0;
            } else if (c == 0x1b) {
                osc3008_filter.esc_seen = 1;
                emit = 0;
            } else {
                emit = 0;                               /* stripped payload */
            }
            if (emit)
                out[out_index++] = c;
            continue;
        }
        if (osc3008_filter.intro_matched > 0 || c == 0x1b) {
            if (c == (unsigned char)OSC3008_INTRO[osc3008_filter.intro_matched]) {
                osc3008_filter.intro_matched++;
                if (osc3008_filter.intro_matched == sizeof(OSC3008_INTRO) - 1) {
                    osc3008_filter.in_sequence = 1;
                    osc3008_filter.intro_matched = 0;
                }
                continue;
            }
            /* Mismatch: the held prefix is ordinary content — emit it, then
             * reprocess this byte (it may itself start a new escape). */
            for (size_t k = 0; k < osc3008_filter.intro_matched; k++)
                out[out_index++] = (unsigned char)OSC3008_INTRO[k];
            osc3008_filter.intro_matched = 0;
            if (c == 0x1b) {
                in_index--;                             /* reprocess as intro */
                continue;
            }
            out[out_index++] = c;
            continue;
        }
        out[out_index++] = c;
    }
    return out_index;
}

/* Authenticate a client without answering it; the caller accepts it only
 * once the connection has somewhere to go. */
static int read_handshake(int client, const char *expected_token,
                          struct handshake *result)
{
    struct uurb_terminal_hello hello;
    struct uurb_terminal_session session = {0, 0};
    char supplied_token[UURB_TERMINAL_TOKEN_LENGTH];
    const char *reject_reason = NULL;
    int64_t deadline = monotonic_milliseconds() + HANDSHAKE_TIMEOUT_MS;

    memset(result, 0, sizeof(*result));
    if (read_exact_deadline(client, &hello, sizeof(hello), deadline) != 1)
        reject_reason = "hello_read";
    else if (ntohl(hello.magic) != UURB_TERMINAL_MAGIC)
        reject_reason = "magic";
    else if (ntohs(hello.version) != UURB_TERMINAL_VERSION &&
             ntohs(hello.version) != UURB_TERMINAL_VERSION_SESSION)
        reject_reason = "version";
    else if (ntohs(hello.token_length) != UURB_TERMINAL_TOKEN_LENGTH)
        reject_reason = "token_length";
    else if (read_exact_deadline(client, supplied_token, sizeof(supplied_token),
                                 deadline) != 1)
        reject_reason = "token_read";
    else if (!constant_time_equal(supplied_token, expected_token,
                                  sizeof(supplied_token)))
        reject_reason = "token_mismatch";
    memset(supplied_token, 0, sizeof(supplied_token));
    if (reject_reason == NULL &&
        ntohs(hello.version) == UURB_TERMINAL_VERSION_SESSION) {
        /* Validate the announced length and role BEFORE reading the name
         * into the fixed stack buffer: name_length arrives from the wire
         * (0-255) and result->name holds MAX_SESSION_NAME+1 bytes. */
        if (read_exact_deadline(client, &session, sizeof(session), deadline) != 1)
            reject_reason = "session_read";
        else if ((session.role != UURB_TERMINAL_ROLE_ATTACH &&
                  session.role != UURB_TERMINAL_ROLE_ANCHOR) ||
                 session.name_length == 0 ||
                 session.name_length > UURB_TERMINAL_MAX_SESSION_NAME)
            reject_reason = "session";
        else if (read_exact_deadline(client, result->name, session.name_length,
                                     deadline) != 1)
            reject_reason = "session_read";
        else if (!session_name_is_valid(result->name, session.name_length))
            reject_reason = "session";
        else
            result->role = session.role;
    }
    if (reject_reason != NULL) {
        /* Phase only: no wire bytes or names — they are unvalidated input. */
        fprintf(stderr, "rejected handshake reason=%s version=%u\n",
                reject_reason, ntohs(hello.version));
        return 0;
    }
    result->version = ntohs(hello.version);
    result->size.ws_col = ntohs(hello.columns);
    result->size.ws_row = ntohs(hello.rows);
    if (result->size.ws_col == 0 || result->size.ws_col > 1000)
        result->size.ws_col = 80;
    if (result->size.ws_row == 0 || result->size.ws_row > 1000)
        result->size.ws_row = 24;
    return 1;
}

static int send_accepted(int client)
{
    const unsigned char accepted = UURB_TERMINAL_ACCEPTED;

    return send_all(client, &accepted, 1);
}

/* Read one client frame and apply it to the PTY. Returns 1 to continue,
 * 0 when the client should be dropped. `detach_on_eof` treats an EOF frame
 * as the viewer leaving rather than as end of the shell's input. */
static int relay_client_frame(int client, int pty_master, int detach_on_eof)
{
    static unsigned char payload[UURB_TERMINAL_MAX_FRAME];
    struct uurb_terminal_frame frame;
    uint32_t length;

    if (read_exact_deadline(client, &frame, sizeof(frame),
                            monotonic_milliseconds() + FRAME_TIMEOUT_MS) != 1)
        return 0;
    if (frame.reserved[0] != 0 || frame.reserved[1] != 0 ||
        frame.reserved[2] != 0)
        return 0;
    length = ntohl(frame.length);
    if (length > UURB_TERMINAL_MAX_FRAME)
        return 0;
    if (length > 0 &&
        read_exact_deadline(client, payload, length,
                            monotonic_milliseconds() + FRAME_TIMEOUT_MS) != 1)
        return 0;
    if (frame.type == UURB_TERMINAL_FRAME_DATA)
        return length > 0 && write_all_fd(pty_master, payload, length);
    if (frame.type == UURB_TERMINAL_FRAME_RESIZE) {
        /* Controllers can emit one out-of-range size transiently while the
         * terminal window initializes (observed: 120x9001 right before the
         * sane 120x30). Dropping the whole connection for it killed the PC
         * terminal; ignore the frame and keep the previous size instead. */
        if (length != 4 || !apply_resize(pty_master, payload)) {
            fprintf(stderr, "terminal resize frame ignored\n");
            return 1;
        }
        return 1;
    }
    if (frame.type == UURB_TERMINAL_FRAME_EOF) {
        unsigned char end_of_input = 4;

        if (length != 0 || detach_on_eof)
            return 0;
        return write_all_fd(pty_master, &end_of_input, 1);
    }
    return 0;
}

/* Version 1: one shell for the lifetime of one connection. */
static int relay_transient(int client, struct winsize initial_size)
{
    struct pollfd descriptors[2];
    unsigned char payload[UURB_TERMINAL_MAX_FRAME];
    pid_t shell_pid;
    int pty_master = -1;
    int status;
    int result = 1;

    if (!send_accepted(client))
        return 1;
    shell_pid = forkpty(&pty_master, NULL, NULL, &initial_size);
    if (shell_pid < 0)
        return 1;
    if (shell_pid == 0)
        run_login_shell();

    fprintf(stderr, "terminal session opened pid=%ld size=%ux%u\n",
            (long)shell_pid, initial_size.ws_col, initial_size.ws_row);
    descriptors[0].fd = client;
    descriptors[0].events = POLLIN;
    descriptors[1].fd = pty_master;
    descriptors[1].events = POLLIN;

    while (!stop_requested) {
        int ready;

        if (waitpid(shell_pid, &status, WNOHANG) == shell_pid) {
            shell_pid = -1;
            result = 0;
            break;
        }
        ready = poll(descriptors, 2, 500);
        if (ready < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        if (ready == 0)
            continue;
        if (descriptors[1].revents & POLLIN) {
            ssize_t size = read(pty_master, payload, sizeof(payload));

            if (size <= 0)
                break;
            /* Version 1 is the path the Windows shim actually takes (no
             * session name), so the OSC 3008 strip must happen here too —
             * filtering hold_session alone left the transient stream
             * polluted with literal-text context signalling. */
            {
                unsigned char filtered[sizeof(payload) + 16];
                size_t filtered_len = osc3008_filter_chunk(
                    payload, (size_t)size, filtered);

                if (filtered_len > 0 &&
                    !send_all_viewer(client, filtered, filtered_len))
                    break;
            }
        }
        if ((descriptors[0].revents & POLLIN) &&
            !relay_client_frame(client, pty_master, 0))
            break;
        if (descriptors[0].revents & (POLLERR | POLLHUP | POLLNVAL))
            break;
        if (descriptors[1].revents & (POLLERR | POLLHUP | POLLNVAL))
            break;
    }

    close(pty_master);
    stop_shell(shell_pid);
    fprintf(stderr, "terminal session closed\n");
    return result;
}

/* A session holder listens on an abstract socket private to this broker;
 * handlers pass it authenticated client sockets with SCM_RIGHTS. */
struct handoff {
    uint8_t role;
    uint8_t reserved;
    uint16_t columns;
    uint16_t rows;
};

static socklen_t session_address(const char *name, struct sockaddr_un *address)
{
    int length;

    memset(address, 0, sizeof(*address));
    address->sun_family = AF_UNIX;
    length = snprintf(address->sun_path + 1, sizeof(address->sun_path) - 1,
                      "uurb-terminal-%ld-%s", (long)broker_pid, name);
    return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + (size_t)length);
}

static int peer_is_same_user(int connection)
{
    struct ucred credentials;
    socklen_t size = sizeof(credentials);

    return getsockopt(connection, SOL_SOCKET, SO_PEERCRED, &credentials, &size) == 0 &&
           credentials.uid == geteuid();
}

static int pass_client(int holder, int client, const struct handoff *message)
{
    char control[CMSG_SPACE(sizeof(int))];
    struct iovec vector = {.iov_base = (void *)message, .iov_len = sizeof(*message)};
    struct msghdr header;
    struct cmsghdr *entry;

    memset(control, 0, sizeof(control));
    memset(&header, 0, sizeof(header));
    header.msg_iov = &vector;
    header.msg_iovlen = 1;
    header.msg_control = control;
    header.msg_controllen = sizeof(control);
    entry = CMSG_FIRSTHDR(&header);
    entry->cmsg_level = SOL_SOCKET;
    entry->cmsg_type = SCM_RIGHTS;
    entry->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(entry), &client, sizeof(int));
    return sendmsg(holder, &header, MSG_NOSIGNAL) == (ssize_t)sizeof(*message);
}

static int receive_client(int connection, struct handoff *message)
{
    char control[CMSG_SPACE(sizeof(int))];
    struct iovec vector = {.iov_base = message, .iov_len = sizeof(*message)};
    struct msghdr header;
    struct cmsghdr *entry;
    int client = -1;

    if (!wait_for_fd(connection, POLLIN, monotonic_milliseconds() + HANDSHAKE_TIMEOUT_MS))
        return -1;
    memset(&header, 0, sizeof(header));
    header.msg_iov = &vector;
    header.msg_iovlen = 1;
    header.msg_control = control;
    header.msg_controllen = sizeof(control);
    if (recvmsg(connection, &header, MSG_CMSG_CLOEXEC) != (ssize_t)sizeof(*message))
        return -1;
    entry = CMSG_FIRSTHDR(&header);
    if (entry != NULL && entry->cmsg_level == SOL_SOCKET &&
        entry->cmsg_type == SCM_RIGHTS && entry->cmsg_len == CMSG_LEN(sizeof(int)))
        memcpy(&client, CMSG_DATA(entry), sizeof(int));
    return client;
}

/* Returning viewers see the current screen only if the program redraws it:
 * nudge the size so the foreground program gets a real SIGWINCH. */
static void request_redraw(int pty_master, unsigned int columns, unsigned int rows)
{
    struct winsize size;

    memset(&size, 0, sizeof(size));
    if (columns == 0 || columns > 4096 || rows == 0 || rows > 4096)
        return;
    size.ws_col = (unsigned short)columns;
    size.ws_row = (unsigned short)(rows > 1 ? rows - 1 : rows + 1);
    ioctl(pty_master, TIOCSWINSZ, &size);
    size.ws_row = (unsigned short)rows;
    ioctl(pty_master, TIOCSWINSZ, &size);
}

static void close_fd(int *fd)
{
    if (*fd >= 0)
        close(*fd);
    *fd = -1;
}

static int hold_session(int listener, int client, const struct handshake *handshake)
{
    struct pollfd descriptors[3 + MAX_ANCHORS];
    unsigned char payload[UURB_TERMINAL_MAX_FRAME];
    struct winsize size = handshake->size;
    int anchors[MAX_ANCHORS];
    int anchor_count = 0;
    int anchored = 0;
    int ever_anchored = 0;
    int attach = client;
    int64_t idle_deadline = 0;
    int64_t redraw_at = monotonic_milliseconds() + REDRAW_DELAY_MS;
    int pty_master = -1;
    int status;
    int index;
    pid_t shell_pid;

    shell_pid = forkpty(&pty_master, NULL, NULL, &size);
    if (shell_pid < 0) {
        close(listener);
        return 1;
    }
    if (shell_pid == 0)
        run_login_shell();
    fprintf(stderr, "terminal session opened pid=%ld size=%ux%u name=%s\n",
            (long)shell_pid, size.ws_col, size.ws_row, handshake->name);

    while (!stop_requested) {
        int count = 2;
        int first_anchor;
        int ready;

        if (waitpid(shell_pid, &status, WNOHANG) == shell_pid) {
            shell_pid = -1;
            break;
        }
        descriptors[0] = (struct pollfd){.fd = listener, .events = POLLIN};
        descriptors[1] = (struct pollfd){.fd = pty_master, .events = POLLIN};
        if (attach >= 0)
            descriptors[count++] = (struct pollfd){.fd = attach, .events = POLLIN};
        for (index = 0; index < anchor_count; index++)
            descriptors[count++] = (struct pollfd){.fd = anchors[index], .events = POLLIN};
        ready = poll(descriptors, (nfds_t)count, redraw_at != 0 ? 100 : 500);
        if (ready < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        /* A viewer may miss output sent before UU finished opening it; ask
         * for one more redraw shortly after every attach. */
        if (redraw_at != 0 && attach >= 0 && monotonic_milliseconds() >= redraw_at) {
            struct winsize current;

            if (ioctl(pty_master, TIOCGWINSZ, &current) == 0)
                request_redraw(pty_master, current.ws_col, current.ws_row);
            redraw_at = 0;
        }
        if (ready == 0)
            continue;

        if (descriptors[1].revents & (POLLIN | POLLERR | POLLHUP)) {
            ssize_t received = read(pty_master, payload, sizeof(payload));

            if (received <= 0)
                break;
            /* No viewer: output is dropped, as with dtach. */
            if (attach >= 0) {
                unsigned char filtered[sizeof(payload) + 16];
                size_t filtered_len = osc3008_filter_chunk(
                    payload, (size_t)received, filtered);

                if (filtered_len > 0 &&
                    !send_all_viewer(attach, filtered, filtered_len))
                    close_viewer(&attach);
            }
        }
        if (attach >= 0 && descriptors[2].fd == attach && descriptors[2].revents) {
            if (!(descriptors[2].revents & POLLIN) ||
                !relay_client_frame(attach, pty_master, 1))
                close_viewer(&attach);
        }
        for (index = anchor_count - 1, first_anchor = count - anchor_count;
             index >= 0; index--) {
            struct pollfd *entry = &descriptors[first_anchor + index];
            char discard[64];

            if (!entry->revents)
                continue;
            ssize_t seen = recv(anchors[index], discard, sizeof(discard),
                                MSG_DONTWAIT);
            if (seen > 0)
                continue;
            if (seen < 0 && (errno == EAGAIN || errno == EWOULDBLOCK ||
                             errno == EINTR))
                continue;
            close(anchors[index]);
            anchors[index] = anchors[--anchor_count];
        }
        if (anchored && anchor_count == 0) {
            fprintf(stderr, "terminal session anchor closed name=%s\n", handshake->name);
            /* A viewer that is still attached keeps the session alive: the
             * pane process can disappear (controller-driven tree cleanup)
             * without the terminal being closed. */
            if (attach < 0)
                break;
            anchored = 0;
        }
        if (!anchored && attach < 0) {
            /* Grace window for sessions that were anchored before: the PC
             * controller's tree cleanup kills the pane proxy mid-session,
             * and a stalled viewer gets kicked — give the client time to
             * reconnect. Sessions never anchored end with their viewer, as
             * before. */
            if (!ever_anchored)
                break;
            if (idle_deadline == 0)
                idle_deadline = monotonic_milliseconds() + idle_grace_ms;
            if (monotonic_milliseconds() >= idle_deadline)
                break;
        } else {
            idle_deadline = 0;
        }

        if (descriptors[0].revents & POLLIN) {
            struct handoff message;
            int connection = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
            int received = -1;

            if (connection >= 0) {
                if (peer_is_same_user(connection))
                    received = receive_client(connection, &message);
                else
                    fprintf(stderr, "terminal connection rejected: peer check\n");
                close(connection);
            }
            if (received < 0) {
                fprintf(stderr, "terminal handshake rejected on a connection\n");
                continue;
            }
            if (message.role == UURB_TERMINAL_ROLE_ATTACH) {
                /* The newest viewer replaces an older one. */
                close_fd(&attach);
                attach = received;
                request_redraw(pty_master, ntohs(message.columns), ntohs(message.rows));
                redraw_at = monotonic_milliseconds() + REDRAW_DELAY_MS;
                fprintf(stderr, "terminal session attached name=%s\n", handshake->name);
            } else if (anchor_count < MAX_ANCHORS) {
                anchors[anchor_count++] = received;
                anchored = 1;
                ever_anchored = 1;
            } else {
                fprintf(stderr, "terminal anchor rejected: anchors=%d limit=%d\n",
                        anchor_count, MAX_ANCHORS);
                send_busy_byte(received);
                close(received);
            }
        }
    }

    close(listener);
    /* Half-close so the viewer's final screen bytes drain before the RST. */
    shutdown(attach, SHUT_WR);
    close_fd(&attach);
    for (index = 0; index < anchor_count; index++)
        close(anchors[index]);
    close(pty_master);
    stop_shell(shell_pid);
    fprintf(stderr, "terminal session closed name=%s\n", handshake->name);
    return 0;
}

/* Version 2: hand the client to the named session, creating it on attach. */
static int route_session(int client, const struct handshake *handshake)
{
    struct sockaddr_un address;
    socklen_t address_size = session_address(handshake->name, &address);
    struct handoff message = {
        .role = handshake->role,
        .columns = htons(handshake->size.ws_col),
        .rows = htons(handshake->size.ws_row),
    };
    int attempt;

    for (attempt = 0; attempt < 3 && !stop_requested; attempt++) {
        int holder = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        int listener;

        if (holder < 0)
            return 1;
        if (connect(holder, (struct sockaddr *)&address, address_size) == 0) {
            int passed = send_accepted(client) && pass_client(holder, client, &message);

            close(holder);
            return passed ? 0 : 1;
        }
        close(holder);
        if (handshake->role != UURB_TERMINAL_ROLE_ATTACH) {
            fprintf(stderr, "rejected terminal bridge anchor reason=no_session\n");
            return 1;
        }
        listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (listener < 0)
            return 1;
        if (bind(listener, (struct sockaddr *)&address, address_size) != 0 ||
            listen(listener, MAX_ANCHORS) != 0) {
            /* Another handler created the session first; join it after a
             * short backoff (50/100/200 ms). */
            fprintf(stderr, "terminal join_after_race attempt=%d\n", attempt);
            close(listener);
            usleep(50000 << attempt);
            continue;
        }
        if (!send_accepted(client)) {
            close(listener);
            return 1;
        }
        return hold_session(listener, client, handshake);
    }
    fprintf(stderr, "terminal join raced three times; refusing with BUSY\n");
    send_busy_byte(client);
    return 1;
}

static int relay_session(int client, const char *expected_token)
{
    struct handshake handshake;

    if (!read_handshake(client, expected_token, &handshake))
        return 1;
    if (handshake.version == UURB_TERMINAL_VERSION_SESSION)
        return route_session(client, &handshake);
    return relay_transient(client, handshake.size);
}

static void reap_handlers(void)
{
    size_t index;
    int status;
    pid_t pid;

    while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
        for (index = 0; index < MAX_SESSIONS; index++) {
            if (handlers[index] == pid) {
                handlers[index] = 0;
                break;
            }
        }
    }
    children_changed = 0;
}

static int available_slot(void)
{
    size_t index;

    for (index = 0; index < MAX_SESSIONS; index++) {
        if (handlers[index] == 0)
            return (int)index;
    }
    return -1;
}

static int active_sessions(void)
{
    int count = 0;

    for (size_t index = 0; index < MAX_SESSIONS; index++) {
        if (handlers[index] != 0)
            count++;
    }
    return count;
}

static void send_busy_byte(int fd)
{
    unsigned char busy = UURB_TERMINAL_BUSY;

    if (fd >= 0)
        send(fd, &busy, sizeof(busy), MSG_NOSIGNAL);
}

static int private_parent_directory(const char *path)
{
    char parent[4096];
    const char *separator;
    size_t parent_length;
    struct stat info;
    int fd;

    if (path == NULL || path[0] != '/' || strlen(path) >= sizeof(parent))
        return 0;
    separator = strrchr(path, '/');
    if (separator == NULL || separator[1] == '\0')
        return 0;
    parent_length = separator == path ? 1 : (size_t)(separator - path);
    if (parent_length >= sizeof(parent))
        return 0;
    memcpy(parent, path, parent_length);
    parent[parent_length] = '\0';
    if (lstat(parent, &info) != 0 || !S_ISDIR(info.st_mode) ||
        info.st_uid != geteuid() || (info.st_mode & 077) != 0)
        return 0;
    fd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0)
        return 0;
    if (fstat(fd, &info) != 0 || !S_ISDIR(info.st_mode) ||
        info.st_uid != geteuid() || (info.st_mode & 077) != 0) {
        close(fd);
        return 0;
    }
    close(fd);
    return 1;
}

static int load_token_file(const char *path, char token[UURB_TERMINAL_TOKEN_LENGTH + 1])
{
    struct stat info;
    unsigned char contents[UURB_TERMINAL_TOKEN_LENGTH + 1];
    size_t expected;
    size_t received = 0;
    int fd;

    if (!private_parent_directory(path))
        return 0;
    fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0 || fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) ||
        info.st_uid != geteuid() || (info.st_mode & 077) != 0 ||
        (info.st_size != UURB_TERMINAL_TOKEN_LENGTH &&
         info.st_size != UURB_TERMINAL_TOKEN_LENGTH + 1)) {
        if (fd >= 0)
            close(fd);
        return 0;
    }
    expected = (size_t)info.st_size;
    while (received < expected) {
        ssize_t count = read(fd, contents + received, expected - received);

        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0) {
            close(fd);
            return 0;
        }
        received += (size_t)count;
    }
    close(fd);
    if (expected == UURB_TERMINAL_TOKEN_LENGTH + 1 &&
        contents[UURB_TERMINAL_TOKEN_LENGTH] != '\n') {
        memset(contents, 0, sizeof(contents));
        return 0;
    }
    contents[UURB_TERMINAL_TOKEN_LENGTH] = '\0';
    memcpy(token, contents, UURB_TERMINAL_TOKEN_LENGTH + 1);
    memset(contents, 0, sizeof(contents));
    return token_is_valid(token);
}

static void remove_matching_file(const char *path, const struct stat *identity);

static int write_ready_file(const char *path, uint16_t port)
{
    char temporary[4096];
    char contents[32];
    struct stat identity;
    int fd;
    int length;

    if (!private_parent_directory(path) || strlen(path) >= sizeof(ready_path) ||
        snprintf(temporary, sizeof(temporary), "%s.%ld.tmp", path,
                 (long)getpid()) >= (int)sizeof(temporary))
        return 0;
    fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd < 0)
        return 0;
    length = snprintf(contents, sizeof(contents), "%u\n", port);
    if (length <= 0 || !write_all_fd(fd, contents, (size_t)length) ||
        fsync(fd) != 0) {
        close(fd);
        unlink(temporary);
        return 0;
    }
    if (fstat(fd, &identity) != 0) {
        close(fd);
        unlink(temporary);
        return 0;
    }
    if (close(fd) != 0) {
        unlink(temporary);
        return 0;
    }
    /* link()+unlink() publishes without replacing an existing or symlink
     * path. A previous crash can leave a stale ready file behind: take it
     * over only when it is our own regular file whose recorded port is no
     * longer served; anything else stays fail-closed. */
    if (link(temporary, path) != 0) {
        struct stat existing;
        char buffer[32] = {0};
        unsigned long stale_port = 0;
        int served = 0;
        int probe_fd;

        if (lstat(path, &existing) != 0 || !S_ISREG(existing.st_mode) ||
            existing.st_uid != geteuid()) {
            fprintf(stderr, "ready file exists and is not a stale own file\n");
            unlink(temporary);
            return 0;
        }
        probe_fd = open(path, O_RDONLY | O_CLOEXEC);
        if (probe_fd >= 0) {
            ssize_t received = read(probe_fd, buffer, sizeof(buffer) - 1);

            close(probe_fd);
            if (received > 0) {
                buffer[received] = '\0';
                stale_port = strtoul(buffer, NULL, 10);
            }
        }
        if (stale_port == 0 || stale_port > 65535) {
            /* Not a broker ready file (no parseable port): foreign file,
             * fail closed exactly like link-publish would. */
            fprintf(stderr, "ready file exists and is not a stale own file\n");
            unlink(temporary);
            return 0;
        }
        if (stale_port <= 65535) {
            struct sockaddr_in probe;
            struct timeval timeout = {1, 0};
            fd_set writable;
            int sock = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);

            memset(&probe, 0, sizeof(probe));
            probe.sin_family = AF_INET;
            probe.sin_port = htons((uint16_t)stale_port);
            probe.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            if (sock >= 0) {
                if (connect(sock, (struct sockaddr *)&probe, sizeof(probe)) == 0) {
                    served = 1;
                } else if (errno == EINPROGRESS) {
                    FD_ZERO(&writable);
                    FD_SET(sock, &writable);
                    if (select(sock + 1, NULL, &writable, NULL, &timeout) > 0) {
                        int so_error = 0;
                        socklen_t length = sizeof(so_error);

                        getsockopt(sock, SOL_SOCKET, SO_ERROR, &so_error, &length);
                        served = so_error == 0;
                    }
                }
                close(sock);
            }
        }
        if (served) {
            fprintf(stderr, "ready file port %lu is served by a live broker\n",
                    stale_port);
            unlink(temporary);
            return 0;
        }
        fprintf(stderr, "taking over a stale ready file (port %lu is dead)\n",
                stale_port);
        unlink(path);
        if (link(temporary, path) != 0) {
            unlink(temporary);
            return 0;
        }
    }
    if (unlink(temporary) != 0) {
        remove_matching_file(path, &identity);
        return 0;
    }
    memcpy(ready_path, path, strlen(path) + 1);
    ready_identity = identity;
    ready_identity_valid = 1;
    return 1;
}

static void remove_matching_file(const char *path, const struct stat *identity)
{
    struct stat current;

    if (lstat(path, &current) == 0 &&
        current.st_dev == identity->st_dev &&
        current.st_ino == identity->st_ino && S_ISREG(current.st_mode) &&
        current.st_uid == geteuid() && (current.st_mode & 077) == 0)
        unlink(path);
}

static void remove_ready_file(void)
{
    if (!ready_identity_valid)
        return;
    remove_matching_file(ready_path, &ready_identity);
    ready_identity_valid = 0;
    memset(ready_path, 0, sizeof(ready_path));
}

int main(int argc, char **argv)
{
    const char *token_file = NULL;
    char expected_token[UURB_TERMINAL_TOKEN_LENGTH + 1];
    const char *ready_file = NULL;
    struct sockaddr_in address;
    socklen_t address_size = sizeof(address);
    struct sigaction action;
    struct pollfd listener_poll;
    int listener = -1;
    int option = 1;
    int index;
    int exit_code = 1;
    int ready_seen = 0;
    int token_seen = 0;

    memset(expected_token, 0, sizeof(expected_token));
    broker_pid = getpid();
    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--ready-file") == 0 && index + 1 < argc &&
            !ready_seen) {
            ready_file = argv[++index];
            ready_seen = 1;
        } else if (strcmp(argv[index], "--token-file") == 0 && index + 1 < argc &&
                   !token_seen) {
            token_file = argv[++index];
            token_seen = 1;
        } else {
            fprintf(stderr, "usage: uu-terminal-bridge --ready-file /absolute/path [--token-file /absolute/path]\n");
            return 2;
        }
    }
    if (ready_file == NULL || !private_parent_directory(ready_file) ||
        (token_file != NULL && !private_parent_directory(token_file))) {
        fprintf(stderr, "usage: uu-terminal-bridge --ready-file /absolute/path [--token-file /absolute/path]\n");
        return 2;
    }
    if (token_file != NULL) {
        if (!load_token_file(token_file, expected_token)) {
            fprintf(stderr, "terminal bridge token file is invalid\n");
            return 2;
        }
    } else {
        const char *environment_token = getenv("UURB_TERMINAL_BRIDGE_TOKEN");

        if (!token_is_valid(environment_token)) {
            fprintf(stderr, "terminal bridge token is not configured\n");
            return 2;
        }
        memcpy(expected_token, environment_token,
               UURB_TERMINAL_TOKEN_LENGTH + 1);
    }
    unsetenv("UURB_TERMINAL_BRIDGE_TOKEN");
    unsetenv("UURB_TERMINAL_BRIDGE_PORT");
    memset(&action, 0, sizeof(action));
    sigemptyset(&action.sa_mask);
    action.sa_handler = handle_signal;
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    sigaction(SIGCHLD, &action, NULL);
    signal(SIGPIPE, SIG_IGN);

    {
        const char *names[] = {"UURB_IO_TIMEOUT_MS",
                               "UURB_VIEWER_SEND_TIMEOUT_MS",
                               "UURB_IDLE_GRACE_MS"};
        int64_t *targets[] = {&io_timeout_ms, &viewer_send_timeout_ms,
                              &idle_grace_ms};
        for (size_t index = 0; index < 3; index++) {
            const char *override = getenv(names[index]);
            unsigned long long value;

            if (override == NULL)
                continue;
            value = strtoul(override, NULL, 10);
            if (value < 100 || value > 86400000) {
                fprintf(stderr,
                        "terminal bridge ignores %s=%s (out of range)\n",
                        names[index], override);
                continue;
            }
            *targets[index] = (int64_t)value;
        }
    }
    /* NOTE: deliberately NOT calling prctl(PR_SET_DUMPABLE, 0) here — with
     * dumpable cleared, /proc/<pid>/fd ownership flips to root and `ss -p`
     * can no longer attribute the listening socket, which breaks the
     * verify.sh terminal checks. Core dumps are restricted system-wide via
     * core_pattern/apport instead. */
    listener = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0)
        goto done;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &option, sizeof(option));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (bind(listener, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        getsockname(listener, (struct sockaddr *)&address, &address_size) != 0 ||
        listen(listener, MAX_SESSIONS) != 0 ||
        !write_ready_file(ready_file, ntohs(address.sin_port)))
        goto done;
    fprintf(stderr, "terminal bridge ready on loopback port %u\n",
            ntohs(address.sin_port));
    listener_poll.fd = listener;
    listener_poll.events = POLLIN;
    exit_code = 0;

    while (!stop_requested) {
        int ready;
        int client;
        int slot;
        pid_t handler;

        if (children_changed)
            reap_handlers();
        ready = poll(&listener_poll, 1, 500);
        if (ready < 0) {
            if (errno == EINTR)
                continue;
            exit_code = 1;
            break;
        }
        if (ready == 0)
            continue;
        client = accept4(listener, NULL, NULL, SOCK_CLOEXEC);
        if (client < 0) {
            if (errno == EINTR)
                continue;
            exit_code = 1;
            break;
        }
        slot = available_slot();
        if (slot < 0) {
            fprintf(stderr, "terminal session rejected: session limit reached"
                            " (active=%d limit=%d)\n",
                    active_sessions(), MAX_SESSIONS);
            send_busy_byte(client);
            close(client);
            continue;
        }
        handler = fork();
        if (handler == 0) {
            int result;

            close(listener);
            result = relay_session(client, expected_token);
            close(client);
            _exit(result);
        }
        close(client);
        if (handler < 0)
            continue;
        handlers[slot] = handler;
    }

done:
    if (listener >= 0)
        close(listener);
    remove_ready_file();
    for (index = 0; index < MAX_SESSIONS; index++) {
        if (handlers[index] > 0)
            kill(handlers[index], SIGTERM);
    }
    for (index = 0; index < MAX_SESSIONS; index++) {
        if (handlers[index] > 0) {
            struct timespec grace = {2, 0};

            nanosleep(&grace, NULL);
            kill(handlers[index], SIGKILL);
            while (waitpid(handlers[index], NULL, 0) < 0 && errno == EINTR)
                ;
        }
    }
    memset(expected_token, 0, sizeof(expected_token));
    return exit_code;
}
