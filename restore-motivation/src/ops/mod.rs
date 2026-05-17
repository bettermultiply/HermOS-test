mod create_firecracker;
mod guest_memory;
mod hash_cpu;
mod netns;
mod noop;
mod snapshot_load;
mod vm_restore;

use libc::{getrusage, rusage, timeval, RUSAGE_SELF, RUSAGE_THREAD};
use std::sync::Arc;
use std::sync::{Condvar, Mutex, OnceLock};
use std::thread;
use std::time::Instant;

#[derive(Clone, Debug)]
pub struct Measurement {
    pub thread_id: usize,
    pub iteration: usize,
    pub instance_id: usize,
    pub stage: &'static str,
    pub start_us: u64,
    pub end_us: u64,
    pub elapsed_us: u64,
    pub user_cpu_us: u64,
    pub sys_cpu_us: u64,
    pub blocked_us: u64,
    pub minflt: u64,
    pub majflt: u64,
    pub nvcsw: u64,
    pub nivcsw: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct RunConfig {
    pub total: usize,
    pub concurrency: usize,
    pub measure_per_op_usage: bool,
}

#[allow(dead_code)]
#[derive(Clone, Copy, Debug)]
pub struct WorkerRunConfig {
    pub thread_id: usize,
    pub iterations: usize,
}

pub type PreparedRunner = Box<dyn FnMut() -> Result<(), String>>;

pub struct RunOutput {
    pub measurements: Vec<Measurement>,
    pub wall_us: u64,
    pub total_usage: UsageDelta,
}

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct UsageSnapshot {
    user_cpu_us: u64,
    sys_cpu_us: u64,
    minflt: u64,
    majflt: u64,
    nvcsw: u64,
    nivcsw: u64,
}

impl UsageSnapshot {
    fn capture(who: i32) -> Result<Self, String> {
        let mut usage = std::mem::MaybeUninit::<rusage>::uninit();
        let ret = unsafe { getrusage(who, usage.as_mut_ptr()) };
        if ret != 0 {
            return Err(format!(
                "getrusage({who}) failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        let usage = unsafe { usage.assume_init() };
        Ok(Self {
            user_cpu_us: timeval_to_micros(usage.ru_utime),
            sys_cpu_us: timeval_to_micros(usage.ru_stime),
            minflt: usage.ru_minflt as u64,
            majflt: usage.ru_majflt as u64,
            nvcsw: usage.ru_nvcsw as u64,
            nivcsw: usage.ru_nivcsw as u64,
        })
    }

    fn delta_since(self, before: Self, elapsed_us: u64) -> UsageDelta {
        let user_cpu_us = self.user_cpu_us.saturating_sub(before.user_cpu_us);
        let sys_cpu_us = self.sys_cpu_us.saturating_sub(before.sys_cpu_us);
        let cpu_us = user_cpu_us.saturating_add(sys_cpu_us);

        UsageDelta {
            user_cpu_us,
            sys_cpu_us,
            blocked_us: elapsed_us.saturating_sub(cpu_us),
            minflt: self.minflt.saturating_sub(before.minflt),
            majflt: self.majflt.saturating_sub(before.majflt),
            nvcsw: self.nvcsw.saturating_sub(before.nvcsw),
            nivcsw: self.nivcsw.saturating_sub(before.nivcsw),
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct UsageDelta {
    pub user_cpu_us: u64,
    pub sys_cpu_us: u64,
    pub blocked_us: u64,
    pub minflt: u64,
    pub majflt: u64,
    pub nvcsw: u64,
    pub nivcsw: u64,
}

impl UsageDelta {
    pub fn zero() -> Self {
        Self::default()
    }

    fn into_measurement(
        self,
        thread_id: usize,
        iteration: usize,
        instance_id: usize,
        stage: &'static str,
        start_us: u64,
        end_us: u64,
    ) -> Measurement {
        Measurement {
            thread_id,
            iteration,
            instance_id,
            stage,
            start_us,
            end_us,
            elapsed_us: end_us.saturating_sub(start_us),
            user_cpu_us: self.user_cpu_us,
            sys_cpu_us: self.sys_cpu_us,
            blocked_us: self.blocked_us,
            minflt: self.minflt,
            majflt: self.majflt,
            nvcsw: self.nvcsw,
            nivcsw: self.nivcsw,
        }
    }
}

pub fn measure_current_thread<F>(run_origin: Instant, mut f: F) -> Result<(u64, u64, UsageDelta), String>
where
    F: FnMut() -> Result<(), String>,
{
    let before = UsageSnapshot::capture(RUSAGE_THREAD)?;
    let start = Instant::now();
    let start_us = start.duration_since(run_origin).as_micros() as u64;
    f()?;
    let elapsed_us = start.elapsed().as_micros() as u64;
    let end_us = start_us.saturating_add(elapsed_us);
    let after = UsageSnapshot::capture(RUSAGE_THREAD)?;
    Ok((start_us, end_us, after.delta_since(before, elapsed_us)))
}

pub fn measure_operation<F>(
    run_origin: Instant,
    measure_per_op_usage: bool,
    mut f: F,
) -> Result<(u64, u64, UsageDelta), String>
where
    F: FnMut() -> Result<(), String>,
{
    if measure_per_op_usage {
        measure_current_thread(run_origin, f)
    } else {
        let start = Instant::now();
        let start_us = start.duration_since(run_origin).as_micros() as u64;
        f()?;
        let end_us = start_us.saturating_add(start.elapsed().as_micros() as u64);
        Ok((start_us, end_us, UsageDelta::zero()))
    }
}

fn capture_current_process_usage() -> Result<UsageSnapshot, String> {
    UsageSnapshot::capture(RUSAGE_SELF)
}

pub(crate) fn capture_run_total_before() -> Result<UsageSnapshot, String> {
    capture_current_process_usage()
}

pub(crate) fn build_run_total_usage(
    before: UsageSnapshot,
    wall_us: u64,
) -> Result<UsageDelta, String> {
    let after = capture_current_process_usage()?;
    Ok(after.delta_since(before, wall_us))
}

fn timeval_to_micros(tv: timeval) -> u64 {
    (tv.tv_sec as u64)
        .saturating_mul(1_000_000)
        .saturating_add(tv.tv_usec as u64)
}

pub enum Op {
    External(fn() -> Result<(), String>),
    ExternalWithPrepare(fn(WorkerRunConfig) -> Result<PreparedRunner, String>),
    Internal(fn(RunConfig) -> Result<RunOutput, String>),
}

pub fn get_op(name: &str) -> Option<Op> {
    match name {
        "noop" => Some(Op::External(noop::run)),
        "hash_cpu" => Some(Op::External(hash_cpu::run)),
        "snapshot_load" => Some(Op::External(snapshot_load::run)),
        "create_firecracker" => Some(Op::ExternalWithPrepare(create_firecracker::prepare)),
        "create_firecracker_full_netns" => Some(Op::ExternalWithPrepare(
            create_firecracker::prepare_full_netns,
        )),
        "guest_memory" => Some(Op::ExternalWithPrepare(guest_memory::prepare)),
        "kvm_create_vm" => Some(Op::ExternalWithPrepare(vm_restore::prepare_kvm)),
        "vm_new" => Some(Op::ExternalWithPrepare(vm_restore::prepare_vm_new)),
        "create_vcpus" => Some(Op::ExternalWithPrepare(vm_restore::prepare_create_vcpus)),
        "restore_memory_regions" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_restore_memory_regions,
        )),
        "restore_vcpu_states" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_restore_vcpu_states,
        )),
        "restore_state" => Some(Op::ExternalWithPrepare(vm_restore::prepare_restore_state)),
        "device_restore" => Some(Op::ExternalWithPrepare(vm_restore::prepare_device_restore)),
        "device_restore_full_netns" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_full_netns,
        )),
        "device_restore_balloon" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_balloon,
        )),
        "device_restore_block" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_block,
        )),
        "device_restore_net" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_net,
        )),
        "device_restore_net_full_netns" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_net_full_netns,
        )),
        "device_restore_vsock" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_vsock,
        )),
        "device_restore_entropy" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_entropy,
        )),
        "device_restore_pmem" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_pmem,
        )),
        "device_restore_virtio_mem" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_device_restore_virtio_mem,
        )),
        "kick_virtio" => Some(Op::ExternalWithPrepare(vm_restore::prepare_kick_virtio)),
        "kick_virtio_full_netns" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_kick_virtio_full_netns,
        )),
        "resume_vcpus" => Some(Op::ExternalWithPrepare(vm_restore::prepare_resume_vcpus)),
        "resume_vcpus_full_netns" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_resume_vcpus_full_netns,
        )),
        "resume_vm" => Some(Op::ExternalWithPrepare(vm_restore::prepare_resume_vm)),
        "resume_vm_full_netns" => Some(Op::ExternalWithPrepare(
            vm_restore::prepare_resume_vm_full_netns,
        )),
        "netns_full_shell" => Some(Op::External(netns::run_full_shell)),
        "netns_step_shell" => Some(Op::Internal(netns::run_step_shell)),
        _ => None,
    }
}

pub fn list_ops() -> &'static [&'static str] {
    &[
        "noop",
        "hash_cpu",
        "snapshot_load",
        "create_firecracker",
        "create_firecracker_full_netns",
        "guest_memory",
        "kvm_create_vm",
        "vm_new",
        "create_vcpus",
        "restore_memory_regions",
        "restore_vcpu_states",
        "restore_state",
        "device_restore",
        "device_restore_full_netns",
        "device_restore_balloon",
        "device_restore_block",
        "device_restore_net",
        "device_restore_net_full_netns",
        "device_restore_vsock",
        "device_restore_entropy",
        "device_restore_pmem",
        "device_restore_virtio_mem",
        "kick_virtio",
        "kick_virtio_full_netns",
        "resume_vcpus",
        "resume_vcpus_full_netns",
        "resume_vm",
        "resume_vm_full_netns",
        "netns_full_shell",
        "netns_step_shell",
    ]
}

fn worker_count(config: RunConfig) -> usize {
    config.concurrency.min(config.total)
}

fn iteration_count(total: usize, workers: usize, tid: usize) -> usize {
    let base = total / workers;
    let remainder = total % workers;
    base + if tid < remainder { 1 } else { 0 }
}

fn iteration_offset(total: usize, workers: usize, tid: usize) -> usize {
    let base = total / workers;
    let remainder = total % workers;
    tid.saturating_mul(base).saturating_add(tid.min(remainder))
}

pub fn run_external(
    op_fn: fn() -> Result<(), String>,
    config: RunConfig,
) -> Result<RunOutput, String> {
    let workers = worker_count(config);
    let gate = Arc::new((
        Mutex::new(StartState {
            ready_workers: 0,
            start: false,
            failure: None,
        }),
        Condvar::new(),
    ));
    let run_origin = Arc::new(OnceLock::new());
    let mut handles = Vec::with_capacity(workers);

    for tid in 0..workers {
        let count = iteration_count(config.total, workers, tid);
        let offset = iteration_offset(config.total, workers, tid);
        let gate = Arc::clone(&gate);
        let run_origin = Arc::clone(&run_origin);
        handles.push(thread::spawn(
            move || -> Result<Vec<Measurement>, String> {
                let mut results = Vec::with_capacity(count);
                let (lock, cvar) = &*gate;
                let mut state = lock.lock().unwrap();
                state.ready_workers += 1;
                cvar.notify_all();

                while !state.start && state.failure.is_none() {
                    state = cvar.wait(state).unwrap();
                }

                if let Some(err) = state.failure.clone() {
                    return Err(err);
                }
                drop(state);

                let run_origin = run_origin
                    .get()
                    .cloned()
                    .ok_or_else(|| "run origin missing".to_string())?;
                for iter in 0..count {
                    let instance_id = offset + iter;
                    let (start_us, end_us, usage) =
                        measure_operation(run_origin, config.measure_per_op_usage, op_fn)?;
                    results.push(usage.into_measurement(
                        tid,
                        iter,
                        instance_id,
                        "run",
                        start_us,
                        end_us,
                    ));
                }
                Ok(results)
            },
        ));
    }

    let (run_start, total_before) = {
        let (lock, cvar) = &*gate;
        let mut state = lock.lock().unwrap();
        while state.ready_workers < workers && state.failure.is_none() {
            state = cvar.wait(state).unwrap();
        }
        let total_before = capture_current_process_usage()?;
        let run_start = Instant::now();
        run_origin
            .set(run_start)
            .map_err(|_| "run origin already initialized".to_string())?;
        if state.failure.is_none() {
            state.start = true;
        }
        cvar.notify_all();
        (run_start, total_before)
    };

    let mut all_results = Vec::with_capacity(config.total);
    for handle in handles {
        let thread_results = handle
            .join()
            .map_err(|_| "worker thread panicked".to_string())??;
        all_results.extend(thread_results);
    }
    let wall_us = run_start.elapsed().as_micros() as u64;
    let total_after = capture_current_process_usage()?;
    Ok(RunOutput {
        measurements: all_results,
        wall_us,
        total_usage: total_after.delta_since(total_before, wall_us),
    })
}

struct StartState {
    ready_workers: usize,
    start: bool,
    failure: Option<String>,
}

struct FinishState {
    finished_workers: usize,
    release_workers: bool,
}

pub fn run_external_with_prepare(
    prepare_fn: fn(WorkerRunConfig) -> Result<PreparedRunner, String>,
    config: RunConfig,
) -> Result<RunOutput, String> {
    let workers = worker_count(config);
    let gate = Arc::new((
        Mutex::new(StartState {
            ready_workers: 0,
            start: false,
            failure: None,
        }),
        Condvar::new(),
    ));
    let finish_gate = Arc::new((
        Mutex::new(FinishState {
            finished_workers: 0,
            release_workers: false,
        }),
        Condvar::new(),
    ));
    let run_origin = Arc::new(OnceLock::new());
    let mut handles = Vec::with_capacity(workers);

    for tid in 0..workers {
        let count = iteration_count(config.total, workers, tid);
        let offset = iteration_offset(config.total, workers, tid);
        let gate = Arc::clone(&gate);
        let finish_gate = Arc::clone(&finish_gate);
        let run_origin = Arc::clone(&run_origin);
        handles.push(thread::spawn(
            move || -> Result<Vec<Measurement>, String> {
                let mut runner = match prepare_fn(WorkerRunConfig {
                    thread_id: tid,
                    iterations: count,
                }) {
                    Ok(runner) => runner,
                    Err(err) => {
                        let (lock, cvar) = &*gate;
                        let mut state = lock.lock().unwrap();
                        if state.failure.is_none() {
                            state.failure = Some(err.clone());
                        }
                        cvar.notify_all();
                        return Err(err);
                    }
                };

                let (lock, cvar) = &*gate;
                let mut state = lock.lock().unwrap();
                state.ready_workers += 1;
                cvar.notify_all();

                while !state.start && state.failure.is_none() {
                    state = cvar.wait(state).unwrap();
                }

                if let Some(err) = state.failure.clone() {
                    return Err(err);
                }
                drop(state);

                let run_origin = run_origin
                    .get()
                    .cloned()
                    .ok_or_else(|| "run origin missing".to_string())?;
                let mut results = Vec::with_capacity(count);
                for iter in 0..count {
                    let instance_id = offset + iter;
                    let (start_us, end_us, usage) =
                        measure_operation(run_origin, config.measure_per_op_usage, || runner())?;
                    results.push(usage.into_measurement(
                        tid,
                        iter,
                        instance_id,
                        "run",
                        start_us,
                        end_us,
                    ));
                }

                let (finish_lock, finish_cvar) = &*finish_gate;
                let mut finish_state = finish_lock.lock().unwrap();
                finish_state.finished_workers += 1;
                finish_cvar.notify_all();

                while !finish_state.release_workers {
                    finish_state = finish_cvar.wait(finish_state).unwrap();
                }

                Ok(results)
            },
        ));
    }

    let (run_start, total_before, started) = {
        let (lock, cvar) = &*gate;
        let mut state = lock.lock().unwrap();
        while state.ready_workers < workers && state.failure.is_none() {
            state = cvar.wait(state).unwrap();
        }
        let total_before = capture_current_process_usage()?;
        let run_start = Instant::now();
        let started = state.failure.is_none();
        if started {
            run_origin
                .set(run_start)
                .map_err(|_| "run origin already initialized".to_string())?;
            state.start = true;
        }
        cvar.notify_all();
        (run_start, total_before, started)
    };

    if started {
        let (finish_lock, finish_cvar) = &*finish_gate;
        let mut finish_state = finish_lock.lock().unwrap();
        while finish_state.finished_workers < workers {
            finish_state = finish_cvar.wait(finish_state).unwrap();
        }

        let wall_us = run_start.elapsed().as_micros() as u64;
        let total_after = capture_current_process_usage();
        finish_state.release_workers = true;
        finish_cvar.notify_all();
        drop(finish_state);

        let total_after = total_after?;
        let mut all_results = Vec::with_capacity(config.total);
        for handle in handles {
            let thread_results = handle
                .join()
                .map_err(|_| "worker thread panicked".to_string())??;
            all_results.extend(thread_results);
        }
        return Ok(RunOutput {
            measurements: all_results,
            wall_us,
            total_usage: total_after.delta_since(total_before, wall_us),
        });
    }

    let mut all_results = Vec::with_capacity(config.total);
    for handle in handles {
        let thread_results = handle
            .join()
            .map_err(|_| "worker thread panicked".to_string())??;
        all_results.extend(thread_results);
    }
    let _ = (run_start, total_before, all_results);
    Err("benchmark preparation failed before start".to_string())
}
