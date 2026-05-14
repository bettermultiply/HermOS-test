#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/wait.h>

#include "workloads.h"

#define DATA_DIR        "/opt/bench/data"
#define ASTROPY_DIR     DATA_DIR "/astropy"
#define FILELIST_PATH   DATA_DIR "/rg-filelist.txt"
#define JSON_PATH       DATA_DIR "/data.json"
#define READLIST_PATH   "/dev/shm/read-list.bin"

#define PYTHON3         "/usr/bin/python3"
#define JSON_TOOL       "/opt/bench/json_tool.py"
#define AGENT_REPLAY    "/opt/bench/agent_replay.py"
#define REPLAY_JSON     "/opt/bench/data/replay.json"
#define REPLAY_WORKDIR  ASTROPY_DIR

/* ── patterns and files for random-rg-scan ── */
static const char *RG_PATTERNS[] = {
    "import", "def", "class", "return", "self",
    "None", "True", "raise", "yield", "lambda",
};
#define N_PATTERNS 10

/* filelist is loaded once at init */
static char  **g_files     = NULL;
static int     g_nfiles    = 0;

/* ══════════════════════════════════════════════════════════════
 * helpers
 * ══════════════════════════════════════════════════════════════ */

static int run_execv(const char *path, char *const argv[]) {
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) { execv(path, argv); _exit(127); }
    int status;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static int run_shell(const char *cmd) {
    char *argv[] = { "/bin/sh", "-c", (char *)cmd, NULL };
    return run_execv("/bin/sh", argv);
}

/*
 * Simple LCG — same sequence for same seed, no libc rand() global state.
 */
static uint32_t lcg_next(uint32_t *state) {
    *state = *state * 1664525u + 1013904223u;
    return *state;
}

/* ══════════════════════════════════════════════════════════════
 * init — load filelist once
 * ══════════════════════════════════════════════════════════════ */
void workloads_init(void) {
    FILE *fp = fopen(FILELIST_PATH, "r");
    if (!fp) return;   /* setup.sh not run yet; workloads will fail gracefully */

    char line[4096];
    int  cap = 256;
    g_files = malloc((size_t)cap * sizeof(char *));
    if (!g_files) { fclose(fp); return; }

    while (fgets(line, sizeof(line), fp)) {
        size_t len = strlen(line);
        if (len && line[len - 1] == '\n') line[--len] = '\0';
        if (!len) continue;

        if (g_nfiles == cap) {
            cap *= 2;
            char **tmp = realloc(g_files, (size_t)cap * sizeof(char *));
            if (!tmp) break;
            g_files = tmp;
        }
        g_files[g_nfiles++] = strdup(line);
    }
    fclose(fp);
}

/* ══════════════════════════════════════════════════════════════
 * health-daemon — immediate return, measures daemon overhead only
 * ══════════════════════════════════════════════════════════════ */
static workload_result_t wl_health_daemon(void) {
    return (workload_result_t){ .ok = 1 };
}

/* ══════════════════════════════════════════════════════════════
 * health-exec — fork/exec /bin/true, measures fork+exec+wait overhead
 * ══════════════════════════════════════════════════════════════ */
static workload_result_t wl_health_exec(void) {
    char *argv[] = { "/bin/true", NULL };
    int rc = run_execv("/bin/true", argv);
    return (workload_result_t){ .ok = (rc == 0) };
}

/* ══════════════════════════════════════════════════════════════
 * python-json-tool
 *   Reads 256MB JSON from disk, does json.load(), counts keys.
 *   Tests large sequential disk read + CPU (JSON parse).
 * ══════════════════════════════════════════════════════════════ */
static workload_result_t wl_python_json_tool(void) {
    char *argv[] = { PYTHON3, JSON_TOOL, JSON_PATH, NULL };
    int rc = run_execv(PYTHON3, argv);
    workload_result_t r = { .ok = (rc == 0) };
    if (!r.ok) snprintf(r.detail, sizeof(r.detail), "exit=%d", rc);
    return r;
}

/* ══════════════════════════════════════════════════════════════
 * cli-pipeline
 *   Mimics a Coding Agent bash tool call:
 *     find astropy -name '*.py' | xargs rg -l 'import' | sort | head -20
 *   Output goes to /dev/null.
 * ══════════════════════════════════════════════════════════════ */
static workload_result_t wl_cli_pipeline(void) {
    const char *cmd =
        "find " ASTROPY_DIR " -name '*.py' -type f"
        " | xargs rg -l 'import'"
        " | sort"
        " | head -20"
        " > /dev/null";
    int rc = run_shell(cmd);
    workload_result_t r = { .ok = (rc == 0) };
    if (!r.ok) snprintf(r.detail, sizeof(r.detail), "exit=%d", rc);
    return r;
}

/* ══════════════════════════════════════════════════════════════
 * random-rg-scan — shared implementation
 *   seed=0      → fixed, deterministic file+pattern selection
 *   seed=random → from /dev/urandom, different pages each run
 *
 *   Selects 5 files and 3 patterns from the candidate lists,
 *   runs rg for each (file, pattern) pair → /dev/null.
 * ══════════════════════════════════════════════════════════════ */
#define RG_N_FILES    5
#define RG_N_PATTERNS 3

static workload_result_t rg_scan(uint32_t seed) {
    if (g_nfiles == 0) {
        workload_result_t r = { .ok = 0 };
        snprintf(r.detail, sizeof(r.detail), "filelist empty, run setup.sh");
        return r;
    }

    uint32_t state = seed;
    int errors = 0;

    for (int fi = 0; fi < RG_N_FILES; fi++) {
        int file_idx = (int)(lcg_next(&state) % (uint32_t)g_nfiles);
        for (int pi = 0; pi < RG_N_PATTERNS; pi++) {
            int pat_idx = (int)(lcg_next(&state) % N_PATTERNS);
            char cmd[8192];
            snprintf(cmd, sizeof(cmd),
                "rg -l '%s' '%s' > /dev/null 2>&1",
                RG_PATTERNS[pat_idx], g_files[file_idx]);
            int rc = run_shell(cmd);
            /* rg exits 1 when no match — that is ok */
            if (rc != 0 && rc != 1) errors++;
        }
    }

    workload_result_t r = { .ok = (errors == 0) };
    if (errors) snprintf(r.detail, sizeof(r.detail), "errors=%d", errors);
    return r;
}

static workload_result_t wl_random_rg_scan_fixed(void) {
    return rg_scan(0);
}

static workload_result_t wl_random_rg_scan_random(void) {
    uint32_t seed = 0;
    int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        if (read(fd, &seed, sizeof(seed)) < 0) seed = (uint32_t)getpid();
        close(fd);
    }
    return rg_scan(seed);
}

/* ══════════════════════════════════════════════════════════════
 * read-list
 *   Reads /dev/shm/read-list.bin sequentially.
 *   Tests large-scale memory page loading from snapshot.
 * ══════════════════════════════════════════════════════════════ */
static workload_result_t wl_read_list(void) {
    int fd = open(READLIST_PATH, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        workload_result_t r = { .ok = 0 };
        snprintf(r.detail, sizeof(r.detail), "open failed: %m");
        return r;
    }

    /* 64KB read buffer — large enough to amortise syscall cost,
     * small enough to stay in L1/L2 and not skew the measurement */
    char buf[65536];
    ssize_t total = 0, n;
    while ((n = read(fd, buf, sizeof(buf))) > 0)
        total += n;
    close(fd);

    workload_result_t r = { .ok = (n == 0) };
    snprintf(r.detail, sizeof(r.detail), "bytes=%zd", total);
    return r;
}

/* ══════════════════════════════════════════════════════════════
 * agent-tool-replay
 *   Replays a recorded coding-agent session against a workspace.
 *   Real shell commands and real file modifications — exercises a
 *   realistic mix of disk I/O, fork/exec, and CPU work.
 *
 *   The workspace is modified in place. If you need repeatability
 *   across runs, restore it externally between measurements.
 * ══════════════════════════════════════════════════════════════ */
static workload_result_t wl_agent_tool_replay(void) {
    char *argv[] = {
        PYTHON3,
        AGENT_REPLAY,
        REPLAY_JSON,
        REPLAY_WORKDIR,
        NULL
    };
    int rc = run_execv(PYTHON3, argv);
    workload_result_t r = { .ok = (rc == 0) };
    if (!r.ok) snprintf(r.detail, sizeof(r.detail), "exit=%d", rc);
    return r;
}

/* ══════════════════════════════════════════════════════════════
 * registry
 * ══════════════════════════════════════════════════════════════ */
static const workload_t WORKLOADS[] = {
    { "health-daemon",         wl_health_daemon         },
    { "health-exec",           wl_health_exec           },
    { "python-json-tool",      wl_python_json_tool      },
    { "cli-pipeline",          wl_cli_pipeline          },
    { "random-rg-scan-fixed",  wl_random_rg_scan_fixed  },
    { "random-rg-scan-random", wl_random_rg_scan_random },
    { "read-list",             wl_read_list             },
    { "agent-tool-replay",     wl_agent_tool_replay     },
};
#define WORKLOAD_COUNT (sizeof(WORKLOADS) / sizeof(WORKLOADS[0]))

const workload_t *workload_find(const char *id) {
    for (size_t i = 0; i < WORKLOAD_COUNT; i++)
        if (strcmp(WORKLOADS[i].id, id) == 0)
            return &WORKLOADS[i];
    return NULL;
}