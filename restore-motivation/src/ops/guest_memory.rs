use std::fmt;
use std::fs::File;
use std::path::{Path, PathBuf};

use vmm::vstate::memory::{self, GuestMemoryState, GuestRegionMmap, MemoryError};

use crate::ops::vm_restore::restore_common::load_microvm_state;
use crate::ops::{PreparedRunner, WorkerRunConfig};

const MEM_FILE_PATH: &str = "snapshots/agent_mem_file";

#[derive(Debug)]
pub enum GuestMemoryFromFileError {
    File(std::io::Error),
    Restore(MemoryError),
}

impl From<std::io::Error> for GuestMemoryFromFileError {
    fn from(err: std::io::Error) -> Self {
        Self::File(err)
    }
}

impl From<MemoryError> for GuestMemoryFromFileError {
    fn from(err: MemoryError) -> Self {
        Self::Restore(err)
    }
}

impl fmt::Display for GuestMemoryFromFileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::File(err) => write!(f, "Failed to load guest memory: {err}"),
            Self::Restore(err) => write!(f, "Failed to restore guest memory: {err}"),
        }
    }
}

impl std::error::Error for GuestMemoryFromFileError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::File(err) => Some(err),
            Self::Restore(err) => Some(err),
        }
    }
}

pub fn guest_memory_from_file(
    mem_file_path: &Path,
    mem_state: &GuestMemoryState,
    track_dirty_pages: bool,
) -> Result<Vec<GuestRegionMmap>, GuestMemoryFromFileError> {
    let mem_file = File::open(mem_file_path)?;
    let guest_mem = memory::snapshot_file(mem_file, mem_state.regions(), track_dirty_pages)?;
    Ok(guest_mem)
}

pub fn prepare(_worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    let microvm_state = load_microvm_state()?;

    let mem_file_path = PathBuf::from(MEM_FILE_PATH);
    Ok(Box::new(move || {
        let _guest_memory = guest_memory_from_file(
            mem_file_path.as_path(),
            &microvm_state.vm_state.memory,
            false,
        )
        .map_err(|err| {
            format!(
                "guest memory load failed for {}: {err}",
                mem_file_path.display()
            )
        })?;

        Ok(())
    }))
}
