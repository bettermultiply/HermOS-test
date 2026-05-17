use std::collections::VecDeque;

use vmm::Vm;

use super::restore_common::{build_vm, load_microvm_state, vcpu_count};
use crate::ops::{PreparedRunner, WorkerRunConfig};

/// Create all vCPUs for a pre-created VM, then drop them.
/// Measures: `Vm::create_vcpus(...)` overhead.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    let microvm_state = load_microvm_state()?;
    let kvm_cap_modifiers = microvm_state.kvm_state.kvm_cap_modifiers.clone();
    let vcpu_count = vcpu_count(&microvm_state)?;
    let mut vms = VecDeque::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        vms.push_back(build_vm(kvm_cap_modifiers.clone())?);
    }

    Ok(Box::new(move || {
        let mut vm: Vm = vms
            .pop_front()
            .ok_or_else(|| "no prepared VM context left".to_string())?;
        let _ = vm
            .create_vcpus(vcpu_count)
            .map_err(|err| format!("Vm::create_vcpus failed: {err}"))?;
        Ok(())
    }))
}
