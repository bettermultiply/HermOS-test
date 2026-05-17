use std::collections::VecDeque;

use vmm::vstate::kvm::Kvm;

use super::restore_common::{build_kvm, load_microvm_state};
use crate::ops::{PreparedRunner, WorkerRunConfig};

/// Create a KVM VM fd from a pre-created KVM handle, then drop it.
/// Measures: `Vm::new(&kvm)` overhead.
pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    let microvm_state = load_microvm_state()?;
    let kvm_cap_modifiers = microvm_state.kvm_state.kvm_cap_modifiers.clone();
    let mut kvms = VecDeque::with_capacity(worker.iterations);

    for _ in 0..worker.iterations {
        kvms.push_back(build_kvm(kvm_cap_modifiers.clone())?);
    }

    Ok(Box::new(move || {
        let kvm: Kvm = kvms
            .pop_front()
            .ok_or_else(|| "no prepared KVM context left".to_string())?;
        let _vm = vmm::Vm::new(&kvm).map_err(|err| format!("Vm::new failed: {err}"))?;
        Ok(())
    }))
}
