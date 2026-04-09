---
name: testvm
description: Use the `testvm` CLI to pack and unpack initrds, build the default BusyBox initrd, and boot Linux kernels under QEMU on `x86_64`, `arm`, or `aarch64`. Use this when the user asks to run, debug, or explain `testvm` commands.
---

# testvm CLI

Use `testvm` directly for kernel/QEMU workflows. The CLI is a thin wrapper over the library API, so the command surface is small and stable.

## Preconditions

Before running commands that touch the host toolchain, assume these external tools are required:

- `cpio`
- `debugfs`
- `gzip`
- `git`
- `docker` with daemon access
- `mkfs.ext4`
- `qemu-system-x86_64`, `qemu-system-arm`, and/or `qemu-system-aarch64`

Runtime facts:

- Python package name: `testvm`
- Console entrypoint: `testvm`
- Supported architectures: `x86_64`, `arm`, `aarch64`
- `arm` means 32-bit ARMv7 hard-float
- Default BusyBox ref: `1_37_stable`

If the user wants isolated cache/build state, set `TESTVM_DATA_DIR` first.

## Data And Work Directories

`testvm` stores cached BusyBox artifacts under `TESTVM_DATA_DIR` when that env var is set.

If `TESTVM_DATA_DIR` is unset:

- On Linux: `~/.local/var/testvm`
- Otherwise: the platformdirs user data directory for `testvm`

Important paths:

- Cached default initrd: `DATA_DIR/busybox/<arch>/<busybox_ref>/initrd.cpio.gz`
- Default BusyBox workdir: `DATA_DIR/work`
- BusyBox source clone: `<workdir>/busybox-src/<busybox_ref>`
- BusyBox build dir: `<workdir>/busybox-build/<arch>/<busybox_ref>`
- BusyBox rootfs dir: `<workdir>/busybox-rootfs/<arch>/<busybox_ref>`

## Module Overlay Merging

`testvm` can merge a module/config overlay onto a base initrd during `testvm run`.

Overlay input may be either:

- a packed initrd file, or
- an unpacked rootfs directory such as `initrd_out/`

Behavior that matters:

- The base initrd is the explicit `--initrd` value, or the default BusyBox initrd if `--initrd` is omitted
- The overlay tree is copied on top of the base tree before boot
- The merged initrd gets a small wrapper `/init` that reads `lib/modules/*/modules.load` and runs `modprobe` for each listed entry before handing off to the original init
- The default BusyBox initrd is built with `modprobe` support for this flow

## Command Reference

### `testvm initrd pack`

Syntax:

```bash
testvm initrd pack ROOTFS_DIR OUTPUT_INITRD
```

Behavior:

- Packs `ROOTFS_DIR` into a `newc` initrd
- Compresses with gzip
- Prints the output path on success
- Fails if the rootfs directory does not exist

Notes:

- Uses `find`, `cpio`, and `gzip`
- Preserves normal file content, symlinks, and executable bits

Example:

```bash
testvm initrd pack ./rootfs ./rootfs.cpio.gz
```

### `testvm initrd unpack`

Syntax:

```bash
testvm initrd unpack INPUT_INITRD OUTPUT_DIR
```

Behavior:

- Unpacks a gzip-compressed or plain `cpio` initrd into `OUTPUT_DIR`
- Prints the output directory on success
- Creates `OUTPUT_DIR` if needed
- Refuses to unpack into a non-empty directory

Example:

```bash
testvm initrd unpack ./rootfs.cpio.gz ./rootfs-unpacked
```

### `testvm initrd build-default`

Syntax:

```bash
testvm initrd build-default --arch ARCH [--output PATH] [--workdir PATH] [--force-rebuild] [--busybox-ref REF]
```

Options:

- `--arch`: required; one of `x86_64`, `arm`, `aarch64`
- `--output`: optional output path; if omitted, uses the cached initrd path
- `--workdir`: optional BusyBox work directory; if omitted, uses `DATA_DIR/work`
- `--force-rebuild`: rebuilds BusyBox/build artifacts instead of reusing cache
- `--busybox-ref`: branch, tag, or commit-ish to clone; defaults to `1_37_stable`

Behavior:

- Reuses a cached initrd when possible
- If `--output` is set and a cached initrd already exists, copies the cached file to the requested output path
- Builds BusyBox inside Docker rather than on the host toolchain
- Prints the final initrd path on success

Builder details that matter operationally:

- Clones BusyBox from `https://git.busybox.net/busybox`
- Builds a static BusyBox initrd
- Writes an `/init` script that mounts `proc`, `sys`, and `devtmpfs`, ensures `/dev/console`, prints `testvm busybox initrd ready`, then execs `/bin/sh`
- Cross-builds `arm` with `arm-linux-gnueabihf-`
- Cross-builds `aarch64` with `aarch64-linux-gnu-`

Examples:

```bash
testvm initrd build-default --arch x86_64
testvm initrd build-default --arch arm
testvm initrd build-default --arch aarch64 --output ./initrd-aarch64.cpio.gz
testvm initrd build-default --arch x86_64 --force-rebuild
```

### `testvm ext4 pack`

Syntax:

```bash
testvm ext4 pack SOURCE_DIR OUTPUT_IMAGE [--size SIZE]
```

Behavior:

- Packs `SOURCE_DIR` into a raw ext4 disk image
- Prints the output image path on success
- Auto-sizes the image if `--size` is omitted
- Fails if the source directory does not exist

Example:

```bash
testvm ext4 pack ./shared ./shared.img
```

### `testvm ext4 unpack`

Syntax:

```bash
testvm ext4 unpack INPUT_IMAGE OUTPUT_DIR
```

Behavior:

- Extracts an ext4 disk image into `OUTPUT_DIR`
- Prints the output directory on success
- Creates `OUTPUT_DIR` if needed
- Refuses to unpack into a non-empty directory

Example:

```bash
testvm ext4 unpack ./shared.img ./shared-out
```

### `testvm run`

Syntax:

```bash
testvm run KERNEL [--arch ARCH] [--initrd PATH] [--workdir PATH] [--gdb-port PORT] [--memory SIZE] [--smp N] [--append ARG]... [--qemu-arg ARG]... [--module-initrd PATH] [--share-dir PATH] [--sync-share-back] [--autorun GUEST_PATH] [--run-host-path HOST_PATH] [--force-rebuild-initrd]
```

Options:

- `KERNEL`: required kernel image path
- `--arch`: overrides architecture detection
- `--initrd`: initrd to boot; if omitted, `testvm` auto-reuses or builds the default BusyBox initrd for the selected architecture
- `--workdir`: BusyBox work directory used when auto-building an initrd
- `--gdb-port`: enables the QEMU gdb stub and starts QEMU halted with `-S -gdb tcp::<port>`
- `--memory`: guest RAM size, default `512M`
- `--smp`: guest vCPU count, default `1`
- `--append`: additional kernel command line arguments; repeat this flag for multiple values
- `--qemu-arg`: additional raw QEMU arguments; repeat this flag for multiple values
- `--module-initrd`: packed initrd file or unpacked rootfs directory to merge onto the base initrd before boot
- `--share-dir`: snapshots a host directory into an ext4 image and mounts it in the guest at `/mnt/testvm-share`
- `--sync-share-back`: extracts the shared ext4 image back into the host directory after QEMU exits
- `--autorun`: absolute guest path to execute after init completes
- `--run-host-path`: convenience flag that infers a share from the host file's parent and autoruns it in the guest
- `--force-rebuild-initrd`: forces a rebuild of the auto-generated initrd

Behavior:

- Validates that the kernel path exists
- If `--arch` is omitted, auto-detects the architecture from the ELF header
- Raw non-ELF kernels cannot be auto-detected; for example, `zImage` requires `--arch arm`
- If `--initrd` is omitted, calls `testvm initrd build-default` logic internally
- If `--module-initrd` is set, `testvm` merges that overlay onto the base initrd before launching QEMU
- The module-loading wrapper reads `lib/modules/*/modules.load` and runs `modprobe` for each listed entry before the original init runs
- If `--share-dir` is used, `testvm` creates a temporary ext4 image and adds it to QEMU as a virtio block drive
- If `--run-host-path` is used without `--share-dir`, the shared directory defaults to the file's parent directory
- The default init script mounts the shared image at `/mnt/testvm-share` and drops to a shell after any autorun program exits
- Returns QEMU's exit code directly

Architecture-specific QEMU launch behavior:

- `x86_64`: uses `qemu-system-x86_64`
- `arm`: uses `qemu-system-arm -machine virt -cpu cortex-a15`
- `aarch64`: uses `qemu-system-aarch64 -machine virt -cpu max`
- All architectures add `-nographic -monitor none -serial stdio`
- Default kernel cmdline always includes `rdinit=/init`
- Console defaults are:
  - `x86_64`: `console=ttyS0`
  - `arm` and `aarch64`: `console=ttyAMA0`

Examples:

```bash
testvm run ./vmlinux
testvm run ./vmlinux --gdb-port 1234 --append panic=-1
testvm run ./vmlinux --memory 1G --smp 2 --qemu-arg -no-reboot
testvm run ./zImage --arch arm
testvm run ./Image --arch aarch64 --initrd ./initrd.cpio.gz
testvm run ./vmlinux --module-initrd ./initrd_out
testvm run ./vmlinux --initrd ./base.cpio.gz --module-initrd ./modules.cpio.gz
testvm run ./vmlinux --share-dir ./shared --autorun /mnt/testvm-share/run.sh
testvm run ./vmlinux --run-host-path ./shared/run.sh
```

## Agent Guidance

Use these defaults unless the user asks for something else:

- Prefer omitting `--arch` for ELF kernels so `testvm` auto-detects the right target
- Add `--arch arm` for raw ARM images such as `zImage`
- Omit `--initrd` unless the user already has a custom initrd
- Use `--module-initrd` when the user has a module/config overlay initrd or unpacked module tree
- Use `--gdb-port 1234` when the user wants to attach GDB before boot
- Use repeated `--append` flags for separate kernel args
- Use repeated `--qemu-arg` flags for raw QEMU passthrough arguments
- Prefer `--run-host-path` for quick "run this script/binary inside the guest" requests
- Add `--sync-share-back` only when the user explicitly wants guest-side changes copied back to the host folder

Examples:

```bash
testvm run ./vmlinux --append panic=-1 --append nokaslr
testvm run ./vmlinux --qemu-arg -no-reboot --qemu-arg -d --qemu-arg int
```

## Failure Semantics

User-facing CLI failures raise a `TestvmError`, print the message in red on stderr, and exit with status `2`.

Common failure cases:

- Unsupported architecture value
- Kernel path does not exist
- Initrd path does not exist
- Ext4 image path does not exist
- Trying to unpack into a non-empty directory
- Missing host tools such as `docker`, `cpio`, `gzip`, `mkfs.ext4`, `debugfs`, or QEMU
- Docker daemon permission errors
- Supplying a non-ELF kernel without `--arch`

For successful `testvm run`, the CLI exits with the same code returned by QEMU.
