use super::command::{detect_upstream_device, run_command};
use super::instance::build_instance;
use super::spec::{cleanup, STAGES};
use crate::ops::{
    build_run_total_usage, capture_run_total_before, measure_operation, Measurement, RunConfig,
    RunOutput,
};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::Instant;

pub fn run(config: RunConfig) -> Result<RunOutput, String> {
    let upstream = detect_upstream_device()?;
    let mut all_results = Vec::with_capacity(config.total * STAGES.len());
    let mut next_seq = 0usize;
    let wall_start = Instant::now();
    let run_origin = wall_start;
    let total_before = capture_run_total_before()?;

    while next_seq < config.total {
        let batch_size = config.concurrency.min(config.total - next_seq);
        let barrier = Arc::new(Barrier::new(batch_size));
        let mut handles = Vec::with_capacity(batch_size);

        for tid in 0..batch_size {
            let instance = build_instance(next_seq + tid);
            let upstream = upstream.clone();
            let barrier = Arc::clone(&barrier);
            let global_iteration = next_seq + tid;
            handles.push(thread::spawn(
                move || -> Result<Vec<Measurement>, String> {
                    let mut results = Vec::with_capacity(STAGES.len());
                    let mut error = None;

                    for stage in STAGES {
                        if error.is_none() {
                            let command = (stage.build)(&instance, &upstream);
                            match measure_operation(run_origin, config.measure_per_op_usage, || {
                                run_command(&command, stage.allow_failure)
                            }) {
                                Ok((start_us, end_us, usage)) => {
                                    results.push(usage.into_measurement(
                                        tid,
                                        global_iteration,
                                        global_iteration,
                                        stage.name,
                                        start_us,
                                        end_us,
                                    ))
                                }
                                Err(err) => error = Some(err),
                            }
                        }
                        barrier.wait();
                    }

                    cleanup(&instance, &upstream);
                    if let Some(err) = error {
                        return Err(err);
                    }
                    Ok(results)
                },
            ));
        }

        for handle in handles {
            let thread_results = handle
                .join()
                .map_err(|_| "worker thread panicked".to_string())??;
            all_results.extend(thread_results);
        }
        next_seq += batch_size;
    }

    let wall_us = wall_start.elapsed().as_micros() as u64;
    Ok(RunOutput {
        measurements: all_results,
        wall_us,
        total_usage: build_run_total_usage(total_before, wall_us)?,
    })
}
