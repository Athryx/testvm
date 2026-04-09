from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from ._arch import Architecture, detect_kernel_arch, normalize_arch
from ._busybox import build_default_initrd
from ._ext4 import pack_ext4_image, unpack_ext4_image
from ._errors import CommandExecutionError, TestvmError

_SHARE_MOUNT_POINT = "/mnt/testvm-share"


def _validate_autorun_path(path: str) -> str:
    if not path.startswith("/"):
        raise TestvmError(f"Autorun path must be absolute inside the guest: {path}")
    if any(character.isspace() for character in path):
        raise TestvmError(f"Autorun path may not contain whitespace: {path}")
    return path


def _resolve_share_configuration(
    *,
    share_dir: Path | None,
    autorun: str | None,
    run_host_path: Path | None,
) -> tuple[Path | None, str | None]:
    resolved_share = None if share_dir is None else share_dir.expanduser().resolve()
    resolved_autorun = None if autorun is None else _validate_autorun_path(autorun)

    if run_host_path is not None:
        if resolved_autorun is not None:
            raise TestvmError("--run-host-path cannot be used together with --autorun")

        host_path = run_host_path.expanduser().resolve()
        if not host_path.is_file():
            raise TestvmError(f"Host autorun path does not exist: {host_path}")

        if resolved_share is None:
            resolved_share = host_path.parent
        if not resolved_share.is_dir():
            raise TestvmError(f"Shared directory does not exist: {resolved_share}")

        try:
            relative = host_path.relative_to(resolved_share)
        except ValueError as exc:
            raise TestvmError(
                f"Host autorun path must be inside the shared directory: {host_path}"
            ) from exc
        resolved_autorun = _validate_autorun_path(
            f"{_SHARE_MOUNT_POINT}/{relative.as_posix()}"
        )

    if resolved_share is not None and not resolved_share.is_dir():
        raise TestvmError(f"Shared directory does not exist: {resolved_share}")

    return resolved_share, resolved_autorun


def _replace_directory_contents(destination: Path, source: Path) -> None:
    for child in destination.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True, copy_function=shutil.copy2)
        elif child.is_symlink():
            target.symlink_to(child.readlink())
        else:
            shutil.copy2(child, target)


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
    share_image: Path | None = None,
    autorun: str | None = None,
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
    if share_image is not None:
        command.extend(["-drive", f"file={share_image},format=raw,if=virtio"])
        cmdline.append("testvm_share=1")
    if autorun is not None:
        cmdline.append(f"testvm_autorun={_validate_autorun_path(autorun)}")

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
    share_dir: str | Path | None = None,
    sync_share_back: bool = False,
    autorun: str | None = None,
    run_host_path: str | Path | None = None,
    force_rebuild_initrd: bool = False,
) -> int:
    kernel_path = Path(kernel).expanduser().resolve()
    if not kernel_path.is_file():
        raise TestvmError(f"Kernel does not exist: {kernel_path}")
    resolved_share_dir, resolved_autorun = _resolve_share_configuration(
        share_dir=None if share_dir is None else Path(share_dir),
        autorun=autorun,
        run_host_path=None if run_host_path is None else Path(run_host_path),
    )
    if sync_share_back and resolved_share_dir is None:
        raise TestvmError("--sync-share-back requires --share-dir or --run-host-path")

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

    with tempfile.TemporaryDirectory(prefix="testvm-share-") as temp_dir:
        share_image: Path | None = None
        if resolved_share_dir is not None:
            share_image = pack_ext4_image(resolved_share_dir, Path(temp_dir) / "share.img")

        command = _build_qemu_command(
            kernel=kernel_path,
            arch=normalized_arch,
            initrd=initrd_path,
            gdb_port=gdb_port,
            memory=memory,
            smp=smp,
            append=append,
            qemu_arg=qemu_arg,
            share_image=share_image,
            autorun=resolved_autorun,
        )
        try:
            result = subprocess.run(command, check=False)
        except FileNotFoundError as exc:
            raise CommandExecutionError(f"Command not found: {command[0]}") from exc

        if sync_share_back and resolved_share_dir is not None and share_image is not None:
            sync_dir = Path(temp_dir) / "share-out"
            unpack_ext4_image(share_image, sync_dir)
            _replace_directory_contents(resolved_share_dir, sync_dir)

        if result.returncode < 0:
            raise CommandExecutionError(f"QEMU exited with signal {-result.returncode}")
        return result.returncode
