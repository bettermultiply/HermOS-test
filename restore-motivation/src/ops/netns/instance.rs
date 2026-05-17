use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

pub struct NetnsInstance {
    pub namespace: String,
    pub host_veth: String,
    pub host_veth_cidr: String,
    pub host_veth_gateway: String,
    pub ns_veth_cidr: String,
    pub veth_network: String,
}

fn run_token() -> &'static str {
    static TOKEN: OnceLock<String> = OnceLock::new();
    TOKEN.get_or_init(|| {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        format!("{:x}", now & 0xfffff)
    })
}

pub fn build_instance(seq: usize) -> NetnsInstance {
    let suffix = format!("{:x}{}", seq, run_token());
    let octet2 = 64 + (seq / 256) % 64;
    let octet3 = seq % 256;
    NetnsInstance {
        namespace: format!("fc{suffix}").chars().take(31).collect(),
        host_veth: format!("fcv{suffix}").chars().take(15).collect(),
        host_veth_cidr: format!("10.{octet2}.{octet3}.1/30"),
        host_veth_gateway: format!("10.{octet2}.{octet3}.1"),
        ns_veth_cidr: format!("10.{octet2}.{octet3}.2/30"),
        veth_network: format!("10.{octet2}.{octet3}.0/30"),
    }
}

#[cfg(test)]
mod tests {
    use super::build_instance;
    use std::collections::HashSet;

    #[test]
    fn host_veth_names_are_short_and_unique_for_parallel_batch() {
        let mut names = HashSet::new();

        for seq in 0..200 {
            let instance = build_instance(seq);
            assert!(instance.host_veth.len() <= 15);
            assert!(names.insert(instance.host_veth));
        }
    }
}
