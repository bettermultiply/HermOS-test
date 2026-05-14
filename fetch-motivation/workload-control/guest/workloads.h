#pragma once

#include <stdint.h>
#include <inttypes.h>

/* ── result type ── */
typedef struct {
    int  ok;
    char detail[256];   /* optional extra info, e.g. lines counted */
} workload_result_t;

/* ── workload descriptor ── */
typedef struct {
    const char        *id;
    workload_result_t (*run)(void);
} workload_t;

/* ── registry ── */
void              workloads_init(void);
const workload_t *workload_find(const char *id);