use super::restore_common::{
    build_paused_vmm_from_snapshot, guest_memory_from_file, load_microvm_state, MEM_FILE_PATH,
};
use crate::ops::netns::vm::VmNetnsContext;
use crate::ops::{PreparedRunner, WorkerRunConfig};
use std::collections::VecDeque;
use std::path::Path;
use std::sync::{Arc, Mutex};
use vmm::Vmm;

#[derive(Clone, Copy)]
enum NetnsMode {
    Minimal,
    Full,
}

struct KickVirtioContext {
    netns: VmNetnsContext,
    vmm: Arc<Mutex<Vmm>>,
}

/// Kick virtio devices on a paused restored VM without resuming vCPUs.
/// Measures: `kick_virtio_devices()`.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, NetnsMode::Minimal)
}

/// Kick virtio devices on a paused restored VM in a full netns topology.
/// Measures: `kick_virtio_devices()`.
pub fn prepare_full_netns(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, NetnsMode::Full)
}

fn prepare_with_netns(worker: WorkerRunConfig, mode: NetnsMode) -> Result<PreparedRunner, String> {
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
        let build_result = build_paused_vmm_from_snapshot(microvm_state, guest_memory);
        let _ = netns.leave();

        let (vmm, _event_manager) = build_result?;
        contexts.push_back(KickVirtioContext { netns, vmm });
    }

    Ok(Box::new(move || {
        let context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared kick_virtio context left".to_string())?;
        let result = (|| {
            context.netns.enter()?;
            context
                .vmm
                .lock()
                .map_err(|_| "VMM lock poisoned".to_string())?
                .kick_virtio_devices_for_resume();
            context.netns.leave()
        })();
        completed_contexts.push(context);
        result
    }))
}
