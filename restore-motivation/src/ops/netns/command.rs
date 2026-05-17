use std::process::Command;

pub fn detect_upstream_device() -> Result<String, String> {
    let output = Command::new("ip")
        .args(["-j", "route", "list", "default"])
        .output()
        .map_err(|err| format!("failed to run ip route: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "ip route failed with status {:?}",
            output.status.code()
        ));
    }

    let stdout = String::from_utf8(output.stdout)
        .map_err(|err| format!("ip route output was not utf8: {err}"))?;
    let marker = "\"dev\":\"";
    let start = stdout
        .find(marker)
        .ok_or_else(|| "could not detect default egress device".to_string())?;
    let rest = &stdout[start + marker.len()..];
    let end = rest
        .find('"')
        .ok_or_else(|| "could not parse default egress device".to_string())?;
    Ok(rest[..end].to_string())
}

pub fn run_command(args: &[String], allow_failure: bool) -> Result<(), String> {
    let status = Command::new(&args[0])
        .args(&args[1..])
        .status()
        .map_err(|err| format!("spawn failed for {}: {err}", args.join(" ")))?;
    if status.success() || allow_failure {
        return Ok(());
    }
    Err(format!(
        "command failed with status {:?}: {}",
        status.code(),
        args.join(" ")
    ))
}

fn privileged_prefix() -> &'static [&'static str] {
    if nix::unistd::geteuid().is_root() {
        &[]
    } else {
        &["sudo", "-n"]
    }
}

fn prefixed(args: &[&str]) -> Vec<String> {
    privileged_prefix()
        .iter()
        .copied()
        .chain(args.iter().copied())
        .map(str::to_string)
        .collect()
}

pub fn netns_add(namespace: &str) -> Vec<String> {
    prefixed(&["ip", "netns", "add", namespace])
}

pub fn host_ip(args: &[&str]) -> Vec<String> {
    let mut parts = vec!["ip"];
    parts.extend_from_slice(args);
    prefixed(&parts)
}

pub fn host_iptables(args: &[&str]) -> Vec<String> {
    let mut parts = vec!["iptables"];
    parts.extend_from_slice(args);
    prefixed(&parts)
}

pub fn netns_ip(namespace: &str, args: &[&str]) -> Vec<String> {
    let mut parts = vec!["ip", "netns", "exec", namespace, "ip"];
    parts.extend_from_slice(args);
    prefixed(&parts)
}

pub fn netns_iptables(namespace: &str, args: &[&str]) -> Vec<String> {
    let mut parts = vec!["ip", "netns", "exec", namespace, "iptables"];
    parts.extend_from_slice(args);
    prefixed(&parts)
}

pub fn netns_del(namespace: &str) -> Vec<String> {
    prefixed(&["ip", "netns", "del", namespace])
}
