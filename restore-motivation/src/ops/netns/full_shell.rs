use super::command::{detect_upstream_device, run_command};
use super::instance::build_instance;
use super::spec::{cleanup, STAGES};
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT_SEQ: AtomicUsize = AtomicUsize::new(0);

pub fn run() -> Result<(), String> {
    let seq = NEXT_SEQ.fetch_add(1, Ordering::Relaxed);
    let instance = build_instance(seq);
    let upstream = detect_upstream_device()?;

    let result = (|| {
        for stage in STAGES {
            let command = (stage.build)(&instance, &upstream);
            run_command(&command, stage.allow_failure)?;
        }
        Ok(())
    })();

    cleanup(&instance, &upstream);
    result
}
