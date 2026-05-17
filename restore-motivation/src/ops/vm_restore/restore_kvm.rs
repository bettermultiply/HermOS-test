use crate::ops::vm_restore::restore_common::load_microvm_state;
use crate::ops::{PreparedRunner, WorkerRunConfig};
use vmm::vstate::kvm::Kvm;

/// Create a KVM handle, then drop it.
/// Measures: `Kvm::new(...)` overhead.
pub fn prepare(_worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    let microvm_state = load_microvm_state()?;
    let kvm_cap_modifiers = microvm_state.kvm_state.kvm_cap_modifiers.clone();

    Ok(Box::new(move || {
        let _kvm =
            Kvm::new(kvm_cap_modifiers.clone()).map_err(|err| format!("Kvm::new failed: {err}"))?;
        Ok(())
    }))
}
