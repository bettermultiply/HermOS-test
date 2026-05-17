use std::collections::VecDeque;
use std::path::Path;

use vmm::vstate::memory::{GuestMemoryState, GuestRegionMmap};
use vmm::Vm;

use super::restore_common::{build_vm, guest_memory_from_file, load_microvm_state, MEM_FILE_PATH};
use crate::ops::{PreparedRunner, WorkerRunConfig};

struct RestoreMemoryRegionsContext {
    vm: Vm,
    guest_memory: Option<Vec<GuestRegionMmap>>,
    mem_state: GuestMemoryState,
}

/// Register restored guest memory regions in a pre-created VM.
/// Measures: `Vm::restore_memory_regions(...)` overhead.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    let microvm_state = load_microvm_state()?;
    let kvm_cap_modifiers = microvm_state.kvm_state.kvm_cap_modifiers.clone();
    let mem_state = microvm_state.vm_state.memory.clone();
    let mem_file_path = Path::new(MEM_FILE_PATH);
    let mut contexts = VecDeque::with_capacity(worker.iterations);
    let mut completed_contexts = Vec::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        contexts.push_back(RestoreMemoryRegionsContext {
            vm: build_vm(kvm_cap_modifiers.clone())?,
            guest_memory: Some(guest_memory_from_file(mem_file_path, &mem_state, false)?),
            mem_state: mem_state.clone(),
        });
    }

    Ok(Box::new(move || {
        let mut context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared restore_memory_regions context left".to_string())?;
        let guest_memory = context
            .guest_memory
            .take()
            .ok_or_else(|| "restore_memory_regions context missing guest memory".to_string())?;
        let result = context
            .vm
            .restore_memory_regions(guest_memory, &context.mem_state)
            .map_err(|err| format!("Vm::restore_memory_regions failed: {err}"));
        completed_contexts.push(context);
        result
    }))
}
