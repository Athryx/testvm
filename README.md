# testvm

`testvm` is a small Python library and CLI for common kernel/QEMU workflows:

- pack a root filesystem directory into a Linux initrd
- unpack an initrd back into a directory
- merge a base initrd with a module/config overlay initrd or rootfs
- build a default BusyBox-based initrd
- run a kernel under QEMU on `x86_64`, `arm`, or `aarch64`

The CLI is a thin wrapper over the public library API, so the same features can be used from other Python packages.

## Install

Runtime dependencies:

- `docker` with access to the Docker daemon
- `git`
- `libmagic` (typically provided by the `file` package)
- `cpio`
- `debugfs`
- `gzip`
- `lz4`
- `mkfs.ext4`
- `qemu-system-x86_64`, `qemu-system-arm`, and/or `qemu-system-aarch64`

Python dependencies are managed through `uv` or standard packaging tools. BusyBox itself is built inside the provided Docker image rather than on the host toolchain, and ARM-family initrds are cross-built there on non-ARM hosts.

## Data Directory

`testvm` uses `TESTVM_DATA_DIR` when set.

If it is unset:

- on Linux, it uses `~/.local/var/testvm`
- on other platforms, it falls back to the platform-specific user data directory from `platformdirs`

BusyBox source/build state and cached default initrds live there by default.
The BusyBox source tree is cloned on the host into that work area and then bind-mounted into the Docker build container.

## CLI

```bash
testvm initrd pack ./rootfs ./rootfs.cpio.gz
testvm initrd unpack ./rootfs.cpio.gz ./rootfs-unpacked
testvm ext4 pack ./shared ./shared.img
testvm ext4 unpack ./shared.img ./shared-out
testvm initrd build-default --arch x86_64
testvm run ./vmlinux --gdb-port 1234 --append panic=-1
testvm run ./vmlinux --nokaslr
testvm initrd build-default --arch arm
testvm run ./zImage
testvm run ./vmlinux --module-initrd ./initrd_out
testvm run ./vmlinux --initrd ./base.cpio.gz --module-initrd ./modules.cpio.gz
testvm run ./vmlinux --share-dir ./shared --autorun-vm-path /mnt/testvm-share/run.sh
testvm run ./vmlinux --share-dir ./shared --share-mode ext4 --sync-share-back
testvm run ./vmlinux --autorun ./shared/run.sh
testvm run ./vmlinux --hostfwd 10022:22
testvm run ./vmlinux --network tap --network-tap tap0testvm --network-host-ip 192.168.10.1
testvm run ./vmlinux --network bridge --network-bridge br0testvm --network-host-ip 192.168.10.1
testvm run ./vmlinux --network none
```

`testvm run` auto-detects the kernel architecture when `--arch` is omitted. ELF kernels are detected from the ELF header; raw kernel images such as ARM `zImage`, arm64 `Image`, and x86 `bzImage` fall back to `file(1)`/`libmagic`. If detection fails, pass `--arch` explicitly. If `--initrd` is omitted, `testvm` reuses or builds a cached BusyBox initrd for the selected architecture.
Use `--nokaslr` to append `nokaslr` to the kernel command line without spelling it through `--append`.
`testvm initrd unpack` accepts gzip-compressed, lz4-compressed, or plain `cpio` initrds.
When `--module-initrd` is provided, `testvm` merges that packed initrd or unpacked rootfs onto the base initrd before boot and installs a small `/init` wrapper that reads `lib/modules/*/modules.load` and runs `modprobe` for each listed entry before handing control to the original init. Packed base or overlay initrds may be gzip, lz4, or plain `cpio`.
When `--share-dir` is provided, `testvm` shares that host directory at `/mnt/testvm-share`. The default `--share-mode initrd` embeds the files directly into the boot initrd, which avoids guest block-device discovery issues. `--share-mode ext4` preserves the previous virtio-block flow and is the only mode that supports `--sync-share-back`. `--autorun` executes a host-side script or binary inside the guest. Without `--share-dir`, only that one file is shared at `/mnt/testvm-share/<name>`; with `--share-dir`, the autorun file must already be inside the shared directory.
Networking defaults to QEMU user-mode NAT. The generated BusyBox initrd brings up `eth0`, uses DHCP by default, and exposes the host as `testvm-host` in user mode. Use repeatable `--hostfwd HOST_PORT:GUEST_PORT` to connect from the host to guest TCP services. `--network tap` and `--network bridge` require preconfigured host networking and can use `--network-host-ip` to expose the host as `testvm-host`; `--network none` disables QEMU networking.

The repo includes `setup_testvm_tap.sh` for a host-only TAP/bridge network and `setup_testvm_tap_forwarding.sh` for a TAP/bridge network with IPv4 forwarding and NAT through the host's default uplink (or an interface name passed as the first argument). The forwarding setup uses bridge `br0testvm`, tap `tap0testvm`, host gateway `192.168.10.1/24`, and installs an `nftables` table named `testvm`. A matching cleanup script is provided as `destroy_testvm_tap_forwarding.sh`.

## Docker Builder

The repo includes a `Dockerfile` based on `ubuntu:24.04`. It installs the BusyBox build dependencies with:

```bash
apt-get update && apt-get install -y --no-install-recommends \
    build-essential crossbuild-essential-arm64 crossbuild-essential-armhf \
    libncurses-dev pkg-config
```

`testvm initrd build-default` builds BusyBox by calling the Docker CLI, bind-mounting the BusyBox source, build directory, and rootfs install directory into the container. `arm` means 32-bit ARMv7 hard-float, while `aarch64` remains the 64-bit ARM target.

## Library API

```python
from testvm import (
    NetworkMode,
    ShareMode,
    build_merged_initrd,
    build_default_initrd,
    pack_ext4_image,
    pack_initrd,
    run_vm,
    unpack_ext4_image,
    unpack_initrd,
)

initrd = build_default_initrd(arch="arm")
pack_initrd("rootfs", "rootfs.cpio.gz")
unpack_initrd("rootfs.cpio.gz", "rootfs-out")
build_merged_initrd(initrd, "initrd_out", output_path="merged.cpio.gz")
pack_ext4_image("shared", "shared.img")
unpack_ext4_image("shared.img", "shared-out")
run_vm(
    kernel="zImage",
    initrd="merged.cpio.gz",
    gdb_port=1234,
    autorun_path="shared/run.sh",
    share_mode=ShareMode.INITRD,
    network=NetworkMode.USER,
    hostfwd=["10022:22"],
)
```
