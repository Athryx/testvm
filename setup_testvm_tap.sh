#!/bin/sh

# 1. Create a new bridge named 'br0'
sudo ip link add br0testvm type bridge

# 2. Assign an IP address to the bridge (This will be the Host's IP)
sudo ip addr add 192.168.10.1/24 dev br0testvm

# 3. Turn the bridge interface "on"
sudo ip link set br0testvm up

# 1. Create a tap device named 'tap0' owned by your current user
sudo ip tuntap add dev tap0testvm mode tap user $(whoami)

# 2. Plug the tap device into the bridge we created earlier
sudo ip link set tap0testvm master br0testvm

# 3. Turn the tap interface "on"
sudo ip link set tap0testvm up
