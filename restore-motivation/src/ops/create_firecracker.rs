use crate::ops::netns::vm::VmNetnsContext;
use crate::ops::{PreparedRunner, WorkerRunConfig};
use std::collections::VecDeque;
use std::fs;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::Duration;

#[derive(Clone, Copy)]
enum NetnsMode {
    None,
    Full,
}

struct FirecrackerContext {
    socket_path: PathBuf,
    netns: Option<VmNetnsContext>,
}

pub fn prepare(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, NetnsMode::None)
}

pub fn prepare_full_netns(worker: WorkerRunConfig) -> Result<PreparedRunner, String> {
    prepare_with_netns(worker, NetnsMode::Full)
}

fn prepare_with_netns(worker: WorkerRunConfig, mode: NetnsMode) -> Result<PreparedRunner, String> {
    let mut contexts = VecDeque::with_capacity(worker.iterations);

    for iteration in 0..worker.iterations {
        let socket_path = build_socket_path(worker.thread_id, iteration);
        remove_socket_if_exists(&socket_path)?;

        let netns = match mode {
            NetnsMode::None => None,
            NetnsMode::Full => Some(VmNetnsContext::prepare_full()?),
        };

        contexts.push_back(FirecrackerContext { socket_path, netns });
    }

    Ok(Box::new(move || {
        let context = contexts
            .pop_front()
            .ok_or_else(|| "no prepared create_firecracker context left".to_string())?;
        run_once(context)
    }))
}

fn run_once(context: FirecrackerContext) -> Result<(), String> {
    let FirecrackerContext { socket_path, netns } = context;

    if let Some(netns) = netns.as_ref() {
        netns.enter()?;
    }

    let run_result = (|| {
        remove_socket_if_exists(&socket_path)?;

        let mut child = spawn_firecracker(&socket_path)?;
        let wait_result = wait_for_api_socket(&socket_path, &mut child);
        let cleanup_result = cleanup_child(&mut child, &socket_path);

        match (wait_result, cleanup_result) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(err), Ok(())) => Err(err),
            (Ok(()), Err(err)) => Err(err),
            (Err(wait_err), Err(cleanup_err)) => {
                Err(format!("{wait_err}; additionally {cleanup_err}"))
            }
        }
    })();

    if let Some(netns) = netns.as_ref() {
        match (run_result, netns.leave()) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(err), Ok(())) => Err(err),
            (Ok(()), Err(err)) => Err(err),
            (Err(run_err), Err(leave_err)) => Err(format!("{run_err}; additionally {leave_err}")),
        }
    } else {
        run_result
    }
}

fn build_socket_path(thread_id: usize, iteration: usize) -> PathBuf {
    PathBuf::from(format!(
        "/tmp/firecracker-api/{}-{}.sock",
        thread_id, iteration
    ))
}

fn remove_socket_if_exists(path: &PathBuf) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(format!(
            "failed to remove stale socket {}: {err}",
            path.display()
        )),
    }
}

fn spawn_firecracker(socket_path: &PathBuf) -> Result<Child, String> {
    Command::new("./firecracker")
        .arg("--api-sock")
        .arg(socket_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| {
            format!(
                "failed to spawn ./firecracker --api-sock {}: {err}",
                socket_path.display()
            )
        })
}

fn wait_for_api_socket(socket_path: &PathBuf, child: &mut Child) -> Result<(), String> {
    const ATTEMPTS: usize = 100;
    const SLEEP_MS: u64 = 10;

    for _ in 0..ATTEMPTS {
        if let Some(status) = child
            .try_wait()
            .map_err(|err| format!("failed to poll firecracker process: {err}"))?
        {
            return Err(format!(
                "firecracker exited before api socket became ready: {status}"
            ));
        }

        if socket_path.exists() {
            match UnixStream::connect(socket_path) {
                Ok(_) => return Ok(()),
                Err(err) => {
                    if is_retryable_socket_error(&err) {
                        thread::sleep(Duration::from_millis(SLEEP_MS));
                        continue;
                    }
                    return Err(format!(
                        "api socket {} exists but cannot be connected: {err}",
                        socket_path.display()
                    ));
                }
            }
        }

        thread::sleep(Duration::from_millis(SLEEP_MS));
    }

    Err(format!(
        "timed out waiting for firecracker api socket {}",
        socket_path.display()
    ))
}

fn is_retryable_socket_error(err: &std::io::Error) -> bool {
    matches!(
        err.kind(),
        std::io::ErrorKind::NotFound
            | std::io::ErrorKind::ConnectionRefused
            | std::io::ErrorKind::ConnectionAborted
            | std::io::ErrorKind::TimedOut
            | std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::Interrupted
    )
}

fn cleanup_child(child: &mut Child, socket_path: &PathBuf) -> Result<(), String> {
    let mut errors = Vec::new();

    match child.try_wait() {
        Ok(Some(_)) => {}
        Ok(None) => {
            if let Err(err) = child.kill() {
                errors.push(format!("failed to kill firecracker process: {err}"));
            }
        }
        Err(err) => errors.push(format!(
            "failed to poll firecracker process before cleanup: {err}"
        )),
    }

    if let Err(err) = child.wait() {
        errors.push(format!("failed to wait firecracker process exit: {err}"));
    }

    if let Err(err) = remove_socket_if_exists(socket_path) {
        errors.push(err);
    }

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}
