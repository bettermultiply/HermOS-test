use std::collections::VecDeque;
use std::path::Path;

use vmm::vstate::vm::VmState;
use vmm::Vm;

#[cfg(target_arch = "aarch64")]
use super::restore_common::construct_kvm_mpidrs;
use super::restore_common::{
    build_vm, guest_memory_from_file, load_microvm_state, restore_vcpu_states, vcpu_count,
    MEM_FILE_PATH,
};
use crate::ops::{PreparedRunner, WorkerRunConfig};

struct RestoreStateContext {
    vm: Vm,
    vm_state: VmState,
    #[cfg(target_arch = "aarch64")]
    mpidrs: Vec<u64>,
}

/// Restore the VM-level KVM state into a prepared VM.
/// Measures: `Vm::restore_state(...)` overhead.
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
        let (mut vcpus, _vcpus_exit_evt) = vm.create_vcpus(vcpu_count).map_err(|err| {
            format!("Vm::create_vcpus failed during restore_state prepare: {err}")
        })?;
        vm.restore_memory_regions(guest_memory, &microvm_state.vm_state.memory)
            .map_err(|err| {
                format!("Vm::restore_memory_regions failed during restore_state prepare: {err}")
            })?;
        restore_vcpu_states(&vm, &mut vcpus, &microvm_state).map_err(|err| {
            format!("restore_vcpu_states failed during restore_state prepare: {err}")
        })?;

        contexts.push_back(RestoreStateContext {
            vm,
            #[cfg(target_arch = "aarch64")]
            mpidrs: construct_kvm_mpidrs(&microvm_state.vcpu_states),
            vm_state: microvm_state.vm_state,
        });
    }

    Ok(Box::new(move || {
        let mut context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared restore_state context left".to_string())?;
        let result = restore_vm_state(&mut context);
        completed_contexts.push(context);
        result
    }))
}

#[cfg(target_arch = "x86_64")]
fn restore_vm_state(context: &mut RestoreStateContext) -> Result<(), String> {
    context
        .vm
        .restore_state(&context.vm_state, false)
        .map_err(|err| format!("Vm::restore_state failed: {err}"))
}

#[cfg(target_arch = "aarch64")]
fn restore_vm_state(context: &mut RestoreStateContext) -> Result<(), String> {
    context
        .vm
        .restore_state(&context.mpidrs, &context.vm_state)
        .map_err(|err| format!("Vm::restore_state failed: {err}"))
}
