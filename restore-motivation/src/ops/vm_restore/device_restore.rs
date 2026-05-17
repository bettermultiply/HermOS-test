use std::collections::VecDeque;
use std::path::Path;

use super::restore_common::{
    build_device_restore_context, guest_memory_from_file, load_microvm_state,
    restore_devices_for_benchmark, DeviceRestoreContext, MEM_FILE_PATH,
};
use crate::ops::netns::vm::VmNetnsContext;
use crate::ops::{PreparedRunner, WorkerRunConfig};
use vmm::BenchmarkDeviceRestoreKind;

#[derive(Clone, Copy)]
enum NetnsMode {
    Minimal,
    Full,
}

struct NetnsDeviceRestoreContext {
    netns: VmNetnsContext,
    context: DeviceRestoreContext,
}

fn prepare_kind(
    worker: WorkerRunConfig,
    kind: BenchmarkDeviceRestoreKind,
) -> Result<PreparedRunner, String> {
    let mem_file_path = Path::new(MEM_FILE_PATH);
    let mut contexts = VecDeque::with_capacity(worker.iterations);
    let mut completed_contexts = Vec::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        let microvm_state = load_microvm_state()?;
        let guest_memory =
            guest_memory_from_file(mem_file_path, &microvm_state.vm_state.memory, false)?;
        contexts.push_back(build_device_restore_context(microvm_state, guest_memory)?);
    }

    Ok(Box::new(move || {
        let mut context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared device_restore context left".to_string())?;
        let result = restore_devices_for_benchmark(&mut context, kind);
        completed_contexts.push(context);
        result
    }))
}

fn prepare_netns_kind(
    worker: WorkerRunConfig,
    kind: BenchmarkDeviceRestoreKind,
    mode: NetnsMode,
) -> Result<PreparedRunner, String> {
    let mem_file_path = Path::new(MEM_FILE_PATH);
    let mut contexts = VecDeque::with_capacity(worker.iterations);
    let mut completed_contexts = Vec::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        let netns = match mode {
            NetnsMode::Minimal => VmNetnsContext::prepare_minimal()?,
            NetnsMode::Full => VmNetnsContext::prepare_full()?,
        };
        netns.enter()?;
        let microvm_state = load_microvm_state()?;
        let guest_memory =
            guest_memory_from_file(mem_file_path, &microvm_state.vm_state.memory, false)?;
        let build_result = build_device_restore_context(microvm_state, guest_memory);
        let _ = netns.leave();

        contexts.push_back(NetnsDeviceRestoreContext {
            netns,
            context: build_result?,
        });
    }

    Ok(Box::new(move || {
        let mut netns_context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared netns device_restore context left".to_string())?;
        let result = (|| {
            netns_context.netns.enter()?;
            let result = restore_devices_for_benchmark(&mut netns_context.context, kind);
            match (result, netns_context.netns.leave()) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(err), Ok(())) => Err(err),
                (Ok(()), Err(leave_err)) => Err(leave_err),
                (Err(err), Err(leave_err)) => Err(format!("{err}; additionally {leave_err}")),
            }
        })();
        completed_contexts.push(netns_context);
        result
    }))
}

pub fn prepare_all(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_netns_kind(worker, BenchmarkDeviceRestoreKind::All, NetnsMode::Minimal)
}

pub fn prepare_all_full_netns(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_netns_kind(worker, BenchmarkDeviceRestoreKind::All, NetnsMode::Full)
}

pub fn prepare_balloon(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_kind(worker, BenchmarkDeviceRestoreKind::Balloon)
}

pub fn prepare_block(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_kind(worker, BenchmarkDeviceRestoreKind::Block)
}

pub fn prepare_net(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_netns_kind(worker, BenchmarkDeviceRestoreKind::Net, NetnsMode::Minimal)
}

pub fn prepare_net_full_netns(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_netns_kind(worker, BenchmarkDeviceRestoreKind::Net, NetnsMode::Full)
}

pub fn prepare_vsock(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_kind(worker, BenchmarkDeviceRestoreKind::Vsock)
}

pub fn prepare_entropy(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_kind(worker, BenchmarkDeviceRestoreKind::Entropy)
}

pub fn prepare_pmem(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_kind(worker, BenchmarkDeviceRestoreKind::Pmem)
}

pub fn prepare_virtio_mem(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_kind(worker, BenchmarkDeviceRestoreKind::VirtioMem)
}
