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

struct ResumeVcpusContext {
    netns: VmNetnsContext,
    vmm: Arc<Mutex<Vmm>>,
}

/// Resume vCPUs of a paused restored VM after virtio devices have already been kicked.
/// Measures: send resume event to vCPUs and check responses.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, NetnsMode::Minimal)
}

/// Resume vCPUs of a paused restored VM in a full netns topology after virtio devices have already
/// been kicked.
/// Measures: send resume event to vCPUs and check responses.
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
        {
            let guard = vmm.lock().map_err(|_| "VMM lock poisoned".to_string())?;
            guard.kick_virtio_devices_for_resume();
        }
        contexts.push_back(ResumeVcpusContext { netns, vmm });
    }

    Ok(Box::new(move || {
        let context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared resume_vcpus context left".to_string())?;
        let result = (|| {
            context.netns.enter()?;
            let result = context
                .vmm
                .lock()
                .map_err(|_| "VMM lock poisoned".to_string())?
                .resume_vcpus_after_kick()
                .map_err(|err| format!("Vmm::resume_vcpus_after_kick failed: {err}"));
            match (result, context.netns.leave()) {
                (Ok(()), Ok(())) => Ok(()),
                (Err(err), Ok(())) => Err(err),
                (Ok(()), Err(leave_err)) => Err(leave_err),
                (Err(err), Err(leave_err)) => Err(format!("{err}; additionally {leave_err}")),
            }
        })();
        completed_contexts.push(context);
        result
    }))
}
