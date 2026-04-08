# testvm

`testvm` is a small Python library and CLI for common kernel/QEMU workflows:

- pack a root filesystem directory into a Linux initrd
- unpack an initrd back into a directory
- build a default BusyBox-based initrd
- run a kernel under QEMU on `x86_64`, `arm`, or `aarch64`

The CLI is a thin wrapper over the public library API, so the same features can be used from other Python packages.

## Install

Runtime dependencies:

- `docker` with access to the Docker daemon
- `git`
- `cpio`
- `gzip`
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
testvm initrd build-default --arch x86_64
testvm run ./vmlinux --gdb-port 1234 --append panic=-1
testvm initrd build-default --arch arm
testvm run ./zImage --arch arm
```

`testvm run` auto-detects the kernel architecture from the ELF header when `--arch` is omitted. Raw ARM kernel images such as `zImage` require `--arch arm`. If `--initrd` is omitted, `testvm` reuses or builds a cached BusyBox initrd for the selected architecture.

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
from testvm import build_default_initrd, pack_initrd, run_vm, unpack_initrd

initrd = build_default_initrd(arch="arm")
pack_initrd("rootfs", "rootfs.cpio.gz")
unpack_initrd("rootfs.cpio.gz", "rootfs-out")
run_vm(kernel="zImage", arch="arm", initrd=initrd, gdb_port=1234)
```
