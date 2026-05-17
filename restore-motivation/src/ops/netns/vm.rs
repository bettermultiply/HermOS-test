use std::fs::File;
use std::os::fd::AsFd;
use std::sync::atomic::{AtomicUsize, Ordering};

use nix::sched::{setns, CloneFlags};

use super::command::{detect_upstream_device, netns_add, netns_del, netns_ip, run_command};
use super::instance::{build_instance, NetnsInstance};
use super::spec::{cleanup, STAGES};

const TAP_NAME: &str = "tap0";
const TAP_CIDR: &str = "172.16.0.1/30";

static NEXT_SEQ: AtomicUsize = AtomicUsize::new(0);

pub struct VmNetnsContext {
    instance: NetnsInstance,
    root_ns: File,
    target_ns: File,
    cleanup: VmNetnsCleanup,
}

enum VmNetnsCleanup {
    Minimal,
    Full { upstream: String },
}

impl VmNetnsContext {
    pub fn prepare_minimal() -> Result<Self, String> {
        let seq = NEXT_SEQ.fetch_add(1, Ordering::Relaxed);
        let instance = build_instance(seq);
        let root_ns = File::open("/proc/self/ns/net")
            .map_err(|err| format!("failed to open current netns fd: {err}"))?;

        let target_ns = (|| {
            run_command(&netns_add(&instance.namespace), false)?;
            run_command(
                &netns_ip(
                    &instance.namespace,
                    &["tuntap", "add", "dev", TAP_NAME, "mode", "tap"],
                ),
                false,
            )?;
            run_command(
                &netns_ip(
                    &instance.namespace,
                    &["addr", "add", TAP_CIDR, "dev", TAP_NAME],
                ),
                false,
            )?;
            run_command(
                &netns_ip(&instance.namespace, &["link", "set", TAP_NAME, "up"]),
                false,
            )?;

            open_named_netns(&instance.namespace)
        })();

        match target_ns {
            Ok(target_ns) => Ok(Self {
                instance,
                root_ns,
                target_ns,
                cleanup: VmNetnsCleanup::Minimal,
            }),
            Err(err) => {
                let _ = run_command(&netns_del(&instance.namespace), true);
                Err(err)
            }
        }
    }

    pub fn prepare_full() -> Result<Self, String> {
        let seq = NEXT_SEQ.fetch_add(1, Ordering::Relaxed);
        let instance = build_instance(seq);
        let root_ns = File::open("/proc/self/ns/net")
            .map_err(|err| format!("failed to open current netns fd: {err}"))?;
        let upstream = detect_upstream_device()?;

        let target_ns = (|| {
            for stage in STAGES {
                let command = (stage.build)(&instance, &upstream);
                run_command(&command, stage.allow_failure)
                    .map_err(|err| format!("netns setup stage {} failed: {err}", stage.name))?;
            }

            open_named_netns(&instance.namespace)
        })();

        match target_ns {
            Ok(target_ns) => Ok(Self {
                instance,
                root_ns,
                target_ns,
                cleanup: VmNetnsCleanup::Full { upstream },
            }),
            Err(err) => {
                cleanup(&instance, &upstream);
                Err(err)
            }
        }
    }

    pub fn enter(&self) -> Result<(), String> {
        setns(self.target_ns.as_fd(), CloneFlags::CLONE_NEWNET)
            .map_err(|err| format!("setns into {} failed: {err}", self.instance.namespace))
    }

    pub fn leave(&self) -> Result<(), String> {
        setns(self.root_ns.as_fd(), CloneFlags::CLONE_NEWNET)
            .map_err(|err| format!("setns back to root netns failed: {err}"))
    }
}

impl Drop for VmNetnsContext {
    fn drop(&mut self) {
        let _ = self.leave();
        match &self.cleanup {
            VmNetnsCleanup::Minimal => {
                let _ = run_command(&netns_del(&self.instance.namespace), true);
            }
            VmNetnsCleanup::Full { upstream } => {
                cleanup(&self.instance, upstream);
            }
        }
    }
}

fn open_named_netns(namespace: &str) -> Result<File, String> {
    for path in [
        format!("/var/run/netns/{namespace}"),
        format!("/run/netns/{namespace}"),
    ] {
        if let Ok(file) = File::open(&path) {
            return Ok(file);
        }
    }

    Err(format!("failed to open named netns fd for {}", namespace))
}
