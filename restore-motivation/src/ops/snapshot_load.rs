use std::fmt;
use std::fs::File;
use std::path::Path;

use vmm::persist::{snapshot_state_sanity_check, MicrovmState, SNAPSHOT_VERSION};
use vmm::snapshot::{Snapshot, SnapshotError};

pub const SNAPSHOT_PATH: &str = "snapshots/agent_snapshot_file";

#[derive(Debug)]
pub enum SnapshotStateFromFileError {
    Open(std::io::Error),
    Load(SnapshotError),
}

impl From<std::io::Error> for SnapshotStateFromFileError {
    fn from(err: std::io::Error) -> Self {
        Self::Open(err)
    }
}

impl From<SnapshotError> for SnapshotStateFromFileError {
    fn from(err: SnapshotError) -> Self {
        Self::Load(err)
    }
}

impl fmt::Display for SnapshotStateFromFileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Open(err) => write!(f, "Failed to open snapshot file: {err}"),
            Self::Load(err) => write!(f, "Failed to load snapshot state from file: {err}"),
        }
    }
}

impl std::error::Error for SnapshotStateFromFileError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Open(err) => Some(err),
            Self::Load(err) => Some(err),
        }
    }
}

pub fn snapshot_state_from_file(
    snapshot_path: &Path,
) -> Result<MicrovmState, SnapshotStateFromFileError> {
    let mut snapshot_reader = File::open(snapshot_path)?;
    let snapshot = Snapshot::load(&mut snapshot_reader)?;

    Ok(snapshot.data)
}

pub fn run() -> Result<(), String> {
    let snapshot_path = Path::new(SNAPSHOT_PATH);
    let microvm_state = snapshot_state_from_file(snapshot_path).map_err(|err| {
        format!(
            "snapshot load failed for {}: {err}",
            snapshot_path.display()
        )
    })?;

    snapshot_state_sanity_check(&microvm_state).map_err(|err| {
        format!(
            "snapshot sanity check failed for {}: {err}",
            snapshot_path.display()
        )
    })?;

    let _ = SNAPSHOT_VERSION;
    Ok(())
}
