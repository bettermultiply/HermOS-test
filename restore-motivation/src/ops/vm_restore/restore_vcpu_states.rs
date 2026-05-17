use std::collections::VecDeque;
use std::path::Path;

use vmm::persist::MicrovmState;
use vmm::{Vcpu, Vm};

use super::restore_common::{
    build_vm, guest_memory_from_file, load_microvm_state, restore_vcpu_states, vcpu_count,
    MEM_FILE_PATH,
};
use crate::ops::{PreparedRunner, WorkerRunConfig};

struct RestoreVcpuStatesContext {
    vm: Vm,
    vcpus: Vec<Vcpu>,
    microvm_state: MicrovmState,
}

/// Restore all vCPU KVM states into a prepared VM.
/// Measures: the vCPU restore loop after `create_vcpus` and `restore_memory_regions`.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    let mem_file_path = Path::new(MEM_FILE_PATH);
    let mut contexts = VecDeque::with_capacity(worker.iterations);
    let mut completed_contexts = Vec::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        let microvm_state = load_microvm_state()?;
        let kvm_cap_modifiers = microvm_state.kvm_state.kvm_cap_modifiers.clone();
        let vcpu_count = vcpu_count(&microvm_state)?;
        let guest_memory =
            guest_memory_from_file(mem_file_path, &microvm_state.vm_state.memory, false)?;

        let mut vm = build_vm(kvm_cap_modifiers)?;
        let (vcpus, _vcpus_exit_evt) = vm.create_vcpus(vcpu_count).map_err(|err| {
            format!("Vm::create_vcpus failed during restore_vcpu_states prepare: {err}")
        })?;
        vm.restore_memory_regions(guest_memory, &microvm_state.vm_state.memory)
            .map_err(|err| {
                format!(
                    "Vm::restore_memory_regions failed during restore_vcpu_states prepare: {err}"
                )
            })?;

        contexts.push_back(RestoreVcpuStatesContext {
            vm,
            vcpus,
            microvm_state,
        });
    }

    Ok(Box::new(move || {
        let mut context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared restore_vcpu_states context left".to_string())?;
        let result = restore_vcpu_states(&context.vm, &mut context.vcpus, &context.microvm_state);
        completed_contexts.push(context);
        result
    }))
}
