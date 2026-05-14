/*
 * bench-daemon: workload benchmarking daemon
 *
 * Listens on a single port (8080).
 * All workloads (health-* and real workloads) share the same request
 * parsing and dispatch code path, so infrastructure page faults are
 * warmed up by health-daemon/health-exec before real workload runs.
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <inttypes.h>
#include <errno.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <sys/resource.h>
#include <sys/epoll.h>
#include <sys/un.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#include "workloads.h"

#define PORT 8080

#define HTTP_OK      "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
#define HTTP_BAD     "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nbad request\n"
#define HTTP_UNKNOWN "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nunknown workload\n"

/* ── timing ── */
static inline int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* ── request parsing ── */
static const char *parse_workload_id(char *req, size_t len,
                                     char *id_buf, size_t id_buf_size) {
    if (len < 16) return NULL;
    if (strncmp(req, "GET /", 5) != 0) return NULL;

    char *path_start = req + 5;
    char *path_end   = memchr(path_start, ' ', len - 5);
    if (!path_end) return NULL;

    size_t id_len = (size_t)(path_end - path_start);
    if (id_len == 0 || id_len >= id_buf_size) return NULL;

    memcpy(id_buf, path_start, id_len);
    id_buf[id_len] = '\0';
    return id_buf;
}

/* ── page fault snapshot ── */
typedef struct { long minflt; long majflt; } flt_t;

static flt_t read_self_faults(void) {
    flt_t f = {0, 0};
    FILE *fp = fopen("/proc/self/stat", "r");
    if (!fp) return f;
    long dummy; char comm[256]; char state;
    int _r = fscanf(fp,
        "%ld %s %c %ld %ld %ld %ld %ld %ld %ld %ld %ld",
        &dummy, comm, &state,
        &dummy, &dummy, &dummy, &dummy, &dummy, &dummy,
        &f.minflt, &dummy, &f.majflt);
    (void)_r;
    fclose(fp);
    return f;
}

/* ── connection handler ── */
static void handle_connection(int fd) {
    char req[4096];
    ssize_t n = recv(fd, req, sizeof(req) - 1, 0);
    if (n <= 0) { close(fd); return; }
    req[n] = '\0';

    char id_buf[256];
    const char *id = parse_workload_id(req, (size_t)n, id_buf, sizeof(id_buf));
    if (!id) {
        send(fd, HTTP_BAD, strlen(HTTP_BAD), MSG_NOSIGNAL);
        close(fd);
        return;
    }

    const workload_t *wl = workload_find(id);
    if (!wl) {
        send(fd, HTTP_UNKNOWN, strlen(HTTP_UNKNOWN), MSG_NOSIGNAL);
        close(fd);
        return;
    }

    /* ── measure ── */
    flt_t   flt_before = read_self_faults();
    int64_t t_start    = now_ns();

    workload_result_t result = wl->run();

    int64_t t_end      = now_ns();
    flt_t   flt_after  = read_self_faults();

    int64_t elapsed_ns = t_end - t_start;
    long    d_minflt   = flt_after.minflt - flt_before.minflt;
    long    d_majflt   = flt_after.majflt - flt_before.majflt;

    /* ── respond ── */
    char resp[512];
    int  resp_len = snprintf(resp, sizeof(resp),
        HTTP_OK
        "workload=%s\n"
        "elapsed_ns=%" PRId64 "\n"
        "minflt=%ld\n"
        "majflt=%ld\n"
        "status=%s\n"
        "%s%s%s",
        id, elapsed_ns, d_minflt, d_majflt,
        result.ok ? "ok" : "error",
        result.detail[0] ? "detail=" : "",
        result.detail[0] ? result.detail : "",
        result.detail[0] ? "\n" : "");

    send(fd, resp, (size_t)resp_len, MSG_NOSIGNAL);
    close(fd);
}

/* ── server socket ── */
static int make_server_socket(int port) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) { perror("socket"); exit(1); }

    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_addr.s_addr = INADDR_ANY,
        .sin_port        = htons((uint16_t)port),
    };
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); exit(1);
    }
    if (listen(fd, 64) < 0) { perror("listen"); exit(1); }
    return fd;
}

/* ── main ── */
int main(void) {
    signal(SIGPIPE, SIG_IGN);

    workloads_init();

    int server_fd = make_server_socket(PORT);

    int epfd = epoll_create1(EPOLL_CLOEXEC);
    if (epfd < 0) { perror("epoll_create1"); exit(1); }

    struct epoll_event ev = { .events = EPOLLIN, .data.fd = server_fd };
    epoll_ctl(epfd, EPOLL_CTL_ADD, server_fd, &ev);

    /* sd_notify without libsystemd */
    const char *notify = getenv("NOTIFY_SOCKET");
    if (notify) {
        int nfd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
        struct sockaddr_un sun;
        memset(&sun, 0, sizeof(sun));
        sun.sun_family = AF_UNIX;
        strncpy(sun.sun_path, notify, sizeof(sun.sun_path) - 1);
        if (notify[0] == '@') sun.sun_path[0] = '\0';
        const char *msg = "READY=1";
        sendto(nfd, msg, strlen(msg), 0,
               (struct sockaddr *)&sun, sizeof(sun));
        close(nfd);
    }

    struct epoll_event events[16];
    for (;;) {
        int nev = epoll_wait(epfd, events, 16, -1);
        for (int i = 0; i < nev; i++) {
            int conn = accept4(server_fd, NULL, NULL, SOCK_CLOEXEC);
            if (conn < 0) continue;
            int flags = fcntl(conn, F_GETFL);
            fcntl(conn, F_SETFL, flags & ~O_NONBLOCK);
            handle_connection(conn);
        }
    }
}