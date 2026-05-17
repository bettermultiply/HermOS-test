// Copyright 2026

use std::env;
use std::fs::OpenOptions;
use std::io::Write;
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

struct BlockTrace {
    path: String,
    drive_filter: Option<String>,
}

static TRACE: OnceLock<Option<BlockTrace>> = OnceLock::new();

fn trace_config() -> Option<&'static BlockTrace> {
    TRACE
        .get_or_init(|| {
            let path = env::var("FC_BLOCK_TRACE_PATH").ok()?;
            let drive_filter = env::var("FC_BLOCK_TRACE_DRIVE").ok();
            Some(BlockTrace { path, drive_filter })
        })
        .as_ref()
}

pub fn record(drive_id: &str, op: &str, offset: u64, length: u32) {
    let Some(config) = trace_config() else {
        return;
    };
    if let Some(filter) = &config.drive_filter
        && filter != drive_id
    {
        return;
    }

    let timestamp_ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);

    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&config.path)
    {
        let _ = writeln!(
            file,
            "{},{},{},{},{}",
            timestamp_ns, drive_id, op, offset, length
        );
    }
}
