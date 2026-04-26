#!/bin/sh

set -eu

BRIDGE_NAME=br0testvm
TAP_NAME=tap0testvm
BRIDGE_CIDR=192.168.10.1/24
GUEST_SUBNET=192.168.10.0/24
UPLINK_IFACE=${1:-$(ip route show default | awk '/default/ {print $5; exit}')}

if [ -z "${UPLINK_IFACE}" ]; then
    echo "error: could not detect the host uplink interface; pass it as the first argument" >&2
    exit 1
fi

if ! command -v nft >/dev/null 2>&1; then
    echo "error: nft is required for forwarding setup" >&2
    exit 1
fi

# 1. Create a bridge that will act as the host-facing gateway for the guest subnet.
sudo ip link add "${BRIDGE_NAME}" type bridge
sudo ip addr add "${BRIDGE_CIDR}" dev "${BRIDGE_NAME}"
sudo ip link set "${BRIDGE_NAME}" up

# 2. Create a TAP device for QEMU and attach it to the bridge.
sudo ip tuntap add dev "${TAP_NAME}" mode tap user "$(whoami)"
sudo ip link set "${TAP_NAME}" master "${BRIDGE_NAME}"
sudo ip link set "${TAP_NAME}" up

# 3. Enable IPv4 forwarding so the host can route packets for the guest subnet.
sudo sysctl -w net.ipv4.ip_forward=1

# 4. Install a dedicated nftables table for NAT and forwarding.
sudo nft delete table ip testvm >/dev/null 2>&1 || true
sudo nft -f - <<EOF
table ip testvm {
    chain forward {
        type filter hook forward priority filter; policy drop;
        iifname "${BRIDGE_NAME}" oifname "${UPLINK_IFACE}" ip saddr ${GUEST_SUBNET} accept
        iifname "${UPLINK_IFACE}" oifname "${BRIDGE_NAME}" ip daddr ${GUEST_SUBNET} ct state related,established accept
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "${UPLINK_IFACE}" ip saddr ${GUEST_SUBNET} masquerade
    }
}
EOF

cat <<EOF
Configured TAP networking with host forwarding.

Bridge:        ${BRIDGE_NAME}
Tap device:    ${TAP_NAME}
Guest subnet:  ${GUEST_SUBNET}
Host gateway:  192.168.10.1
Uplink iface:  ${UPLINK_IFACE}

Use testvm with:
  testvm run ./vmlinux \\
    --network tap \\
    --network-tap ${TAP_NAME} \\
    --network-ip 192.168.10.2/24 \\
    --network-gateway 192.168.10.1 \\
    --network-dns 1.1.1.1 \\
    --network-host-ip 192.168.10.1
EOF
