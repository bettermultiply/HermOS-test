use std::fs::File;
use std::path::Path;
use std::sync::{Arc, Mutex};

use vmm::builder::build_microvm_from_snapshot;
use vmm::cpu_config::templates::{CpuTemplateType, KvmCapability};
use vmm::persist::MicrovmState;
use vmm::resources::VmResources;
use vmm::seccomp::get_empty_filters;
use vmm::vmm_config::instance_info::InstanceInfo;
use vmm::vstate::kvm::Kvm;
use vmm::vstate::memory::{self, GuestMemoryState, GuestRegionMmap};
use vmm::vstate::vcpu::Vcpu;
use vmm::{BenchmarkDeviceRestoreKind, EventManager, Vm, Vmm};
use vmm_sys_util::eventfd::EventFd;

use crate::ops::snapshot_load::{snapshot_state_from_file, SNAPSHOT_PATH};

pub const MEM_FILE_PATH: &str = "snapshots/agent_mem_file";

pub fn load_microvm_state() -> Result<MicrovmState, String> {
    let snapshot_path = Path::new(SNAPSHOT_PATH);
    snapshot_state_from_file(snapshot_path).map_err(|err| {
        format!(
            "snapshot load failed for {}: {err}",
            snapshot_path.display()
        )
    })
}

pub fn guest_memory_from_file(
    mem_file_path: &Path,
    mem_state: &GuestMemoryState,
    track_dirty_pages: bool,
) -> Result<Vec<GuestRegionMmap>, String> {
    let mem_file = File::open(mem_file_path).map_err(|err| {
        format!(
            "failed to open guest memory file {}: {err}",
            mem_file_path.display()
        )
    })?;
    memory::snapshot_file(mem_file, mem_state.regions(), track_dirty_pages).map_err(|err| {
        format!(
            "failed to restore guest memory from {}: {err}",
            mem_file_path.display()
        )
    })
}

pub fn build_kvm(kvm_cap_modifiers: Vec<KvmCapability>) -> Result<Kvm, String> {
    Kvm::new(kvm_cap_modifiers).map_err(|err| format!("Kvm::new failed: {err}"))
}

pub fn build_vm(kvm_cap_modifiers: Vec<KvmCapability>) -> Result<Vm, String> {
    let kvm = build_kvm(kvm_cap_modifiers)?;
    Vm::new(&kvm).map_err(|err| format!("Vm::new failed: {err}"))
}

pub fn vcpu_count(microvm_state: &MicrovmState) -> Result<u8, String> {
    u8::try_from(microvm_state.vcpu_states.len()).map_err(|_| {
        format!(
            "vcpu count {} exceeds u8::MAX",
            microvm_state.vcpu_states.len()
        )
    })
}

pub fn instance_info() -> InstanceInfo {
    InstanceInfo {
        id: "bench".to_string(),
        state: Default::default(),
        vmm_version: "bench".to_string(),
        app_name: "fc-restore-bench".to_string(),
    }
}

pub fn vm_resources_from_state(microvm_state: &MicrovmState) -> Result<VmResources, String> {
    let mut vm_resources = VmResources::default();
    vm_resources.machine_config.vcpu_count = vcpu_count(microvm_state)?;
    vm_resources.machine_config.mem_size_mib = usize::try_from(microvm_state.vm_info.mem_size_mib)
        .map_err(|_| {
            format!(
                "mem_size_mib {} exceeds usize::MAX",
                microvm_state.vm_info.mem_size_mib
            )
        })?;
    vm_resources.machine_config.smt = microvm_state.vm_info.smt;
    vm_resources.machine_config.cpu_template =
        Some(CpuTemplateType::Static(microvm_state.vm_info.cpu_template));
    vm_resources.machine_config.huge_pages = microvm_state.vm_info.huge_pages;
    vm_resources.boot_source.config = microvm_state.vm_info.boot_source.clone();
    Ok(vm_resources)
}

pub fn event_manager() -> Result<EventManager, String> {
    EventManager::new().map_err(|err| format!("EventManager::new failed: {err}"))
}

pub fn build_paused_vmm_from_snapshot(
    microvm_state: MicrovmState,
    guest_memory: Vec<GuestRegionMmap>,
) -> Result<(Arc<Mutex<Vmm>>, EventManager), String> {
    let instance_info = instance_info();
    let mut event_manager = event_manager()?;
    let seccomp_filters = get_empty_filters();
    let mut vm_resources = vm_resources_from_state(&microvm_state)?;

    let vmm = build_microvm_from_snapshot(
        &instance_info,
        &mut event_manager,
        microvm_state,
        guest_memory,
        None,
        &seccomp_filters,
        &mut vm_resources,
        false,
    )
    .map_err(|err| format!("build_microvm_from_snapshot failed: {err}"))?;

    Ok((vmm, event_manager))
}

pub struct DeviceRestoreContext {
    pub vm: Arc<Vm>,
    pub vcpus_exit_evt: EventFd,
    pub microvm_state: MicrovmState,
    pub vm_resources: VmResources,
    pub event_manager: EventManager,
}

pub fn build_device_restore_context(
    microvm_state: MicrovmState,
    guest_memory: Vec<GuestRegionMmap>,
) -> Result<DeviceRestoreContext, String> {
    let kvm = build_kvm(microvm_state.kvm_state.kvm_cap_modifiers.clone())?;
    let mut vm = Vm::new(&kvm).map_err(|err| format!("Vm::new failed: {err}"))?;
    let vcpu_count = vcpu_count(&microvm_state)?;
    let (mut vcpus, vcpus_exit_evt) = vm
        .create_vcpus(vcpu_count)
        .map_err(|err| format!("Vm::create_vcpus failed: {err}"))?;
    vm.restore_memory_regions(guest_memory, &microvm_state.vm_state.memory)
        .map_err(|err| format!("Vm::restore_memory_regions failed: {err}"))?;
    restore_vcpu_states(&vm, &mut vcpus, &microvm_state)
        .map_err(|err| format!("restore_vcpu_states failed: {err}"))?;
    #[cfg(target_arch = "x86_64")]
    vm.restore_state(&microvm_state.vm_state, false)
        .map_err(|err| format!("Vm::restore_state failed: {err}"))?;
    #[cfg(target_arch = "aarch64")]
    {
        let mpidrs = construct_kvm_mpidrs(&microvm_state.vcpu_states);
        vm.restore_state(&mpidrs, &microvm_state.vm_state)
            .map_err(|err| format!("Vm::restore_state failed: {err}"))?;
    }

    Ok(DeviceRestoreContext {
        vm: Arc::new(vm),
        vcpus_exit_evt,
        vm_resources: vm_resources_from_state(&microvm_state)?,
        microvm_state,
        event_manager: event_manager()?,
    })
}

pub fn restore_devices_for_benchmark(
    context: &mut DeviceRestoreContext,
    kind: BenchmarkDeviceRestoreKind,
) -> Result<(), String> {
    vmm::restore_devices_for_benchmark(
        &context.vm,
        &mut context.event_manager,
        &context.vcpus_exit_evt,
        &mut context.vm_resources,
        &context.microvm_state,
        kind,
        "bench",
    )
}

#[cfg(target_arch = "x86_64")]
pub fn restore_vcpu_states(
    _vm: &Vm,
    vcpus: &mut [Vcpu],
    microvm_state: &MicrovmState,
) -> Result<(), String> {
    if let Some(state_tsc) = microvm_state
        .vcpu_states
        .first()
        .and_then(|state| state.tsc_khz)
    {
        if let Some(first_vcpu) = vcpus.first() {
            let scaling_required = first_vcpu
                .kvm_vcpu
                .is_tsc_scaling_required(state_tsc)
                .map_err(|err| format!("KvmVcpu::is_tsc_scaling_required failed: {err}"))?;
            if scaling_required {
                for vcpu in vcpus.iter() {
                    vcpu.kvm_vcpu
                        .set_tsc_khz(state_tsc)
                        .map_err(|err| format!("KvmVcpu::set_tsc_khz failed: {err}"))?;
                }
            }
        }
    }

    for (vcpu, state) in vcpus.iter_mut().zip(microvm_state.vcpu_states.iter()) {
        vcpu.kvm_vcpu
            .restore_state(state)
            .map_err(|err| format!("KvmVcpu::restore_state failed: {err}"))?;
    }

    Ok(())
}

#[cfg(target_arch = "aarch64")]
pub fn restore_vcpu_states(
    _vm: &Vm,
    vcpus: &mut [Vcpu],
    microvm_state: &MicrovmState,
) -> Result<(), String> {
    for (vcpu, state) in vcpus.iter_mut().zip(microvm_state.vcpu_states.iter()) {
        vcpu.kvm_vcpu
            .restore_state(state)
            .map_err(|err| format!("KvmVcpu::restore_state failed: {err}"))?;
    }

    Ok(())
}

#[cfg(target_arch = "aarch64")]
pub fn construct_kvm_mpidrs(vcpu_states: &[vmm::vstate::vcpu::VcpuState]) -> Vec<u64> {
    vcpu_states
        .iter()
        .map(|state| {
            let cpu_affid = ((state.mpidr & 0xFF_0000_0000) >> 8) | (state.mpidr & 0xFF_FFFF);
            cpu_affid << 32
        })
        .collect()
}
