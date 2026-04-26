#!/bin/sh

# 1. Delete the TAP device
sudo ip link delete tap0testvm

# 2. Delete the Bridge
sudo ip link delete br0testvm
