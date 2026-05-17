mod create_vcpus;
mod device_restore;
mod kick_virtio;
pub(crate) mod restore_common;
mod restore_kvm;
mod restore_memory_regions;
mod restore_state;
mod restore_vcpu_states;
mod resume_vcpus;
mod resume_vm;
mod vm_new;

pub use create_vcpus::prepare as prepare_create_vcpus;
pub use device_restore::{
    prepare_all as prepare_device_restore,
    prepare_all_full_netns as prepare_device_restore_full_netns,
    prepare_balloon as prepare_device_restore_balloon,
    prepare_block as prepare_device_restore_block,
    prepare_entropy as prepare_device_restore_entropy, prepare_net as prepare_device_restore_net,
    prepare_net_full_netns as prepare_device_restore_net_full_netns,
    prepare_pmem as prepare_device_restore_pmem,
    prepare_virtio_mem as prepare_device_restore_virtio_mem,
    prepare_vsock as prepare_device_restore_vsock,
};
pub use kick_virtio::prepare as prepare_kick_virtio;
pub use kick_virtio::prepare_full_netns as prepare_kick_virtio_full_netns;
pub use restore_kvm::prepare as prepare_kvm;
pub use restore_memory_regions::prepare as prepare_restore_memory_regions;
pub use restore_state::prepare as prepare_restore_state;
pub use restore_vcpu_states::prepare as prepare_restore_vcpu_states;
pub use resume_vcpus::prepare as prepare_resume_vcpus;
pub use resume_vcpus::prepare_full_netns as prepare_resume_vcpus_full_netns;
pub use resume_vm::prepare as prepare_resume_vm;
pub use resume_vm::prepare_full_netns as prepare_resume_vm_full_netns;
pub use vm_new::prepare as prepare_vm_new;
