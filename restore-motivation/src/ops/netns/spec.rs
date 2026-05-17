use super::command::{
    host_ip, host_iptables, netns_add as command_netns_add, netns_del, netns_ip, netns_iptables,
    run_command,
};
use super::instance::NetnsInstance;

const TAP_NAME: &str = "tap0";
const NS_VETH_NAME: &str = "veth0";
const TAP_CIDR: &str = "172.16.0.1/30";
const TAP_NAT_CIDR: &str = "172.16.0.1/30";

pub struct Stage {
    pub name: &'static str,
    pub allow_failure: bool,
    pub build: fn(&NetnsInstance, &str) -> Vec<String>,
}

pub const STAGES: &[Stage] = &[
    Stage {
        name: "netns_add",
        allow_failure: false,
        build: netns_add,
    },
    Stage {
        name: "tap_add",
        allow_failure: false,
        build: tap_add,
    },
    Stage {
        name: "tap_addr",
        allow_failure: false,
        build: tap_addr,
    },
    Stage {
        name: "tap_up",
        allow_failure: false,
        build: tap_up,
    },
    Stage {
        name: "veth_add",
        allow_failure: false,
        build: veth_add,
    },
    Stage {
        name: "ns_veth_addr",
        allow_failure: false,
        build: ns_veth_addr,
    },
    Stage {
        name: "ns_veth_up",
        allow_failure: false,
        build: ns_veth_up,
    },
    Stage {
        name: "host_veth_addr",
        allow_failure: false,
        build: host_veth_addr,
    },
    Stage {
        name: "host_veth_up",
        allow_failure: false,
        build: host_veth_up,
    },
    Stage {
        name: "route_add",
        allow_failure: false,
        build: route_add,
    },
    Stage {
        name: "ns_nat",
        allow_failure: false,
        build: ns_nat,
    },
    Stage {
        name: "host_nat",
        allow_failure: false,
        build: host_nat,
    },
    Stage {
        name: "forward_accept",
        allow_failure: false,
        build: forward_accept,
    },
];

fn netns_add(instance: &NetnsInstance, _: &str) -> Vec<String> {
    command_netns_add(&instance.namespace)
}

fn tap_add(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_ip(
        &instance.namespace,
        &["tuntap", "add", "dev", TAP_NAME, "mode", "tap"],
    )
}

fn tap_addr(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_ip(
        &instance.namespace,
        &["addr", "add", TAP_CIDR, "dev", TAP_NAME],
    )
}

fn tap_up(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_ip(&instance.namespace, &["link", "set", TAP_NAME, "up"])
}

fn veth_add(instance: &NetnsInstance, _: &str) -> Vec<String> {
    host_ip(&[
        "link",
        "add",
        "name",
        &instance.host_veth,
        "type",
        "veth",
        "peer",
        "name",
        NS_VETH_NAME,
        "netns",
        &instance.namespace,
    ])
}

fn ns_veth_addr(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_ip(
        &instance.namespace,
        &["addr", "add", &instance.ns_veth_cidr, "dev", NS_VETH_NAME],
    )
}

fn ns_veth_up(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_ip(
        &instance.namespace,
        &["link", "set", "dev", NS_VETH_NAME, "up"],
    )
}

fn host_veth_addr(instance: &NetnsInstance, _: &str) -> Vec<String> {
    host_ip(&[
        "addr",
        "add",
        &instance.host_veth_cidr,
        "dev",
        &instance.host_veth,
    ])
}

fn host_veth_up(instance: &NetnsInstance, _: &str) -> Vec<String> {
    host_ip(&["link", "set", "dev", &instance.host_veth, "up"])
}

fn route_add(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_ip(
        &instance.namespace,
        &[
            "route",
            "add",
            "default",
            "via",
            &instance.host_veth_gateway,
        ],
    )
}

fn ns_nat(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_iptables(
        &instance.namespace,
        &[
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-s",
            TAP_NAT_CIDR,
            "-o",
            NS_VETH_NAME,
            "-j",
            "MASQUERADE",
        ],
    )
}

fn host_nat(instance: &NetnsInstance, upstream: &str) -> Vec<String> {
    host_iptables(&[
        "-t",
        "nat",
        "-A",
        "POSTROUTING",
        "-s",
        &instance.veth_network,
        "-o",
        upstream,
        "-j",
        "MASQUERADE",
    ])
}

fn forward_accept(instance: &NetnsInstance, _: &str) -> Vec<String> {
    netns_iptables(&instance.namespace, &["-P", "FORWARD", "ACCEPT"])
}

fn delete_host_nat(instance: &NetnsInstance, upstream: &str) -> Vec<String> {
    host_iptables(&[
        "-t",
        "nat",
        "-D",
        "POSTROUTING",
        "-s",
        &instance.veth_network,
        "-o",
        upstream,
        "-j",
        "MASQUERADE",
    ])
}

pub fn cleanup(instance: &NetnsInstance, upstream: &str) {
    let commands = [
        delete_host_nat(instance, upstream),
        host_ip(&["link", "del", &instance.host_veth]),
        netns_del(&instance.namespace),
    ];

    for command in commands {
        let _ = run_command(&command, true);
    }
}
