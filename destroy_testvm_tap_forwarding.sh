#!/bin/sh

set -eu

TAP_NAME=tap0testvm
BRIDGE_NAME=br0testvm

sudo nft delete table ip testvm >/dev/null 2>&1 || true
sudo ip link delete "${TAP_NAME}" >/dev/null 2>&1 || true
sudo ip link delete "${BRIDGE_NAME}" >/dev/null 2>&1 || true

echo "Removed testvm TAP forwarding setup."
