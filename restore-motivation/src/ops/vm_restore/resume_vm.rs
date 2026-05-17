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
enum ResumeVmNetnsMode {
    Minimal,
    Full,
}

struct ResumeVmContext {
    netns: VmNetnsContext,
    vmm: Option<Arc<Mutex<Vmm>>>,
    _event_manager: event_manager::EventManager<Arc<Mutex<dyn event_manager::MutEventSubscriber>>>,
}

/// Resume a paused restored VM.
/// Measures: `kick_virtio_devices()` + send/check vcpu resume path.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, ResumeVmNetnsMode::Minimal)
}

/// Resume a paused restored VM after preparing the complete netns topology.
/// Measures: `kick_virtio_devices()` + send/check vcpu resume path.
pub fn prepare_full_netns(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, ResumeVmNetnsMode::Full)
}

fn prepare_with_netns(
    worker: WorkerRunConfig,
    mode: ResumeVmNetnsMode,
) -> Result<PreparedRunner, String> {
    let mem_file_path = Path::new(MEM_FILE_PATH);
    let mut contexts = VecDeque::with_capacity(worker.iterations);
    let mut completed_contexts = Vec::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        let netns = match mode {
            ResumeVmNetnsMode::Minimal => VmNetnsContext::prepare_minimal()?,
            ResumeVmNetnsMode::Full => VmNetnsContext::prepare_full()?,
        };
        netns.enter()?;
        let microvm_state = load_microvm_state()?;
        let guest_memory =
            guest_memory_from_file(mem_file_path, &microvm_state.vm_state.memory, false)?;
        let build_result = build_paused_vmm_from_snapshot(microvm_state, guest_memory);
        let _ = netns.leave();

        let (vmm, event_manager) = build_result?;
        contexts.push_back(ResumeVmContext {
            netns,
            vmm: Some(vmm),
            _event_manager: event_manager,
        });
    }

    Ok(Box::new(move || {
        let mut context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared resume_vm context left".to_string())?;
        let result = (|| {
            context.netns.enter()?;
            let vmm = context
                .vmm
                .take()
                .ok_or_else(|| "resume_vm context missing VMM".to_string())?;
            let result = {
                let mut guard = vmm.lock().map_err(|_| "VMM lock poisoned".to_string())?;
                guard
                    .resume_vm()
                    .map_err(|err| format!("Vmm::resume_vm failed: {err}"))
            };
            match (result, context.netns.leave()) {
                (Ok(()), Ok(())) => {
                    // Keep the resumed VMM alive until after the measured operation returns so
                    // the per-op timing reflects only the resume path, not VMM teardown.
                    context.vmm = Some(vmm);
                    Ok(())
                }
                (Err(err), Ok(())) => Err(err),
                (Ok(()), Err(leave_err)) => Err(leave_err),
                (Err(err), Err(leave_err)) => Err(format!("{err}; additionally {leave_err}")),
            }
        })();

        completed_contexts.push(context);

        result
    }))
}
