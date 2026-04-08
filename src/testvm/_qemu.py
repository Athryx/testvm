from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from ._arch import Architecture, detect_kernel_arch, normalize_arch
from ._busybox import build_default_initrd
from ._errors import CommandExecutionError, TestvmError


def _build_qemu_command(
    *,
    kernel: Path,
    arch: Architecture | str,
    initrd: Path | None,
    gdb_port: int | None,
    memory: str,
    smp: int,
    append: Iterable[str],
    qemu_arg: Iterable[str],
) -> list[str]:
    arch = normalize_arch(arch)
    if arch is Architecture.X86_64:
        command = [
            "qemu-system-x86_64",
            "-m",
            memory,
            "-smp",
            str(smp),
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(kernel),
        ]
        cmdline = ["console=ttyS0", "rdinit=/init"]
    elif arch is Architecture.ARM:
        command = [
            "qemu-system-arm",
            "-machine",
            "virt",
            "-cpu",
            "cortex-a15",
            "-m",
            memory,
            "-smp",
            str(smp),
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(kernel),
        ]
        cmdline = ["console=ttyAMA0", "rdinit=/init"]
    elif arch is Architecture.AARCH64:
        command = [
            "qemu-system-aarch64",
            "-machine",
            "virt",
            "-cpu",
            "max",
            "-m",
            memory,
            "-smp",
            str(smp),
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(kernel),
        ]
        cmdline = ["console=ttyAMA0", "rdinit=/init"]
    else:
        raise TestvmError(f"Unsupported architecture: {arch}")

    if initrd is not None:
        command.extend(["-initrd", str(initrd)])
    if gdb_port is not None:
        command.extend(["-S", "-gdb", f"tcp::{gdb_port}"])

    cmdline.extend(str(item) for item in append)
    command.extend(["-append", " ".join(cmdline)])
    command.extend(str(item) for item in qemu_arg)
    return command


def run_vm(
    *,
    kernel: str | Path,
    arch: Architecture | str | None = None,
    initrd: str | Path | None = None,
    gdb_port: int | None = None,
    memory: str = "512M",
    smp: int = 1,
    append: Iterable[str] = (),
    qemu_arg: Iterable[str] = (),
    workdir: str | Path | None = None,
    force_rebuild_initrd: bool = False,
) -> int:
    kernel_path = Path(kernel).expanduser().resolve()
    if not kernel_path.is_file():
        raise TestvmError(f"Kernel does not exist: {kernel_path}")

    normalized_arch = (
        detect_kernel_arch(kernel_path) if arch is None else normalize_arch(arch)
    )

    initrd_path: Path | None
    if initrd is not None:
        initrd_path = Path(initrd).expanduser().resolve()
        if not initrd_path.is_file():
            raise TestvmError(f"Initrd does not exist: {initrd_path}")
    else:
        initrd_path = build_default_initrd(
            arch=normalized_arch,
            workdir=workdir,
            force_rebuild=force_rebuild_initrd,
        )

    command = _build_qemu_command(
        kernel=kernel_path,
        arch=normalized_arch,
        initrd=initrd_path,
        gdb_port=gdb_port,
        memory=memory,
        smp=smp,
        append=append,
        qemu_arg=qemu_arg,
    )
    result = subprocess.run(command, check=False)
    if result.returncode < 0:
        raise CommandExecutionError(f"QEMU exited with signal {-result.returncode}")
    return result.returncode
