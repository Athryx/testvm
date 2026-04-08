from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ._arch import Architecture, normalize_arch
from ._errors import CommandExecutionError, TestvmError
from ._initrd import pack_initrd
from ._paths import get_data_dir

BUSYBOX_GIT_URL = "https://git.busybox.net/busybox"
DEFAULT_BUSYBOX_REF = "1_37_stable"
DOCKER_IMAGE_TAG = "testvm-busybox-builder:ubuntu24.04"
DOCKER_SOURCE_DIR = Path("/workspace/source")
DOCKER_BUILD_DIR = Path("/workspace/build")
DOCKER_ROOTFS_DIR = Path("/workspace/rootfs")
_BUSYBOX_MAKE_VARS = {
    Architecture.X86_64: [],
    Architecture.ARM: ["ARCH=arm", "CROSS_COMPILE=arm-linux-gnueabihf-"],
    Architecture.AARCH64: ["ARCH=arm64", "CROSS_COMPILE=aarch64-linux-gnu-"],
}


def _run_checked(command: list[str], *, cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise CommandExecutionError(f"Command not found: {command[0]}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        message = stderr or stdout or "command failed"
        raise CommandExecutionError(f"{' '.join(command)}: {message}")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _docker_bind_mount(source: Path, target: Path) -> str:
    return f"type=bind,src={source},dst={target}"


def _docker_user_args() -> list[str]:
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return []
    return ["--user", f"{getuid()}:{getgid()}"]


def _run_docker_checked(command: list[str]) -> None:
    try:
        _run_checked(command)
    except CommandExecutionError as exc:
        message = str(exc)
        if "Command not found: docker" in message:
            raise TestvmError(
                "Docker CLI not found; install Docker and ensure `docker` is on PATH"
            ) from exc
        if "permission denied while trying to connect to the docker API" in message:
            raise TestvmError(
                "Docker daemon access failed; ensure your user can access the Docker socket"
            ) from exc
        raise


def _ensure_docker_builder_image() -> None:
    repo_root = _repo_root()
    dockerfile = repo_root / "Dockerfile"
    _run_docker_checked(
        [
            "docker",
            "build",
            "--tag",
            DOCKER_IMAGE_TAG,
            "--file",
            str(dockerfile),
            str(repo_root),
        ]
    )


def _build_busybox_in_docker(
    *,
    arch: Architecture,
    source_dir: Path,
    build_dir: Path,
    rootfs_dir: Path,
) -> None:
    make_vars = " ".join(_BUSYBOX_MAKE_VARS[arch])
    make_prefix = f"make -C {DOCKER_SOURCE_DIR} O={DOCKER_BUILD_DIR}"
    if make_vars:
        make_prefix = f"{make_prefix} {make_vars}"
    extra_config_steps: list[str] = []
    if arch is not Architecture.X86_64:
        extra_config_steps.extend(
            [
                f"if grep -q '^CONFIG_SHA1_HWACCEL=y$' {DOCKER_BUILD_DIR}/.config; then",
                f"    sed -i 's/^CONFIG_SHA1_HWACCEL=y$/# CONFIG_SHA1_HWACCEL is not set/' {DOCKER_BUILD_DIR}/.config",
                "fi",
                f"if grep -q '^CONFIG_SHA256_HWACCEL=y$' {DOCKER_BUILD_DIR}/.config; then",
                f"    sed -i 's/^CONFIG_SHA256_HWACCEL=y$/# CONFIG_SHA256_HWACCEL is not set/' {DOCKER_BUILD_DIR}/.config",
                "fi",
            ]
        )
    build_steps = "\n".join(
        [
            "set -eu",
            f"{make_prefix} defconfig",
            f"if grep -q '^# CONFIG_STATIC is not set$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^# CONFIG_STATIC is not set$/CONFIG_STATIC=y/' {DOCKER_BUILD_DIR}/.config",
            "elif ! grep -q '^CONFIG_STATIC=y$' "
            f"{DOCKER_BUILD_DIR}/.config; then",
            f"    printf '\\nCONFIG_STATIC=y\\n' >> {DOCKER_BUILD_DIR}/.config",
            "fi",
            f"if grep -q '^CONFIG_TC=y$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^CONFIG_TC=y$/# CONFIG_TC is not set/' {DOCKER_BUILD_DIR}/.config",
            "fi",
            *extra_config_steps,
            f"{make_prefix} silentoldconfig",
            f"{make_prefix} -j{os.cpu_count() or 1}",
            f"{make_prefix} CONFIG_PREFIX={DOCKER_ROOTFS_DIR} install",
        ]
    )

    command = [
        "docker",
        "run",
        "--rm",
        *_docker_user_args(),
        "--mount",
        _docker_bind_mount(source_dir, DOCKER_SOURCE_DIR),
        "--mount",
        _docker_bind_mount(build_dir, DOCKER_BUILD_DIR),
        "--mount",
        _docker_bind_mount(rootfs_dir, DOCKER_ROOTFS_DIR),
        DOCKER_IMAGE_TAG,
        "sh",
        "-lc",
        build_steps,
    ]
    _run_docker_checked(command)


def _ensure_busybox_source(source_dir: Path, busybox_ref: str) -> None:
    if source_dir.exists():
        return

    source_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            busybox_ref,
            BUSYBOX_GIT_URL,
            str(source_dir),
        ]
    )


def _write_init_script(rootfs_dir: Path) -> None:
    init_path = rootfs_dir / "init"
    script = """#!/bin/sh
set -eu

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true

mkdir -p /dev /proc /sys /run /tmp /root
[ -c /dev/console ] || mknod -m 600 /dev/console c 5 1

export PATH=/bin:/sbin:/usr/bin:/usr/sbin
echo "testvm busybox initrd ready"
exec /bin/sh
"""
    init_path.write_text(script)
    init_path.chmod(0o755)


def build_default_initrd(
    *,
    arch: Architecture | str,
    output_path: str | Path | None = None,
    workdir: str | Path | None = None,
    force_rebuild: bool = False,
    busybox_ref: str = DEFAULT_BUSYBOX_REF,
) -> Path:
    normalized_arch = normalize_arch(arch)

    data_dir = get_data_dir()
    cache_root = data_dir / "busybox" / normalized_arch / busybox_ref
    cache_root.mkdir(parents=True, exist_ok=True)
    cached_initrd = cache_root / "initrd.cpio.gz"
    if workdir is None:
        work_path = data_dir / "work"
    else:
        work_path = Path(workdir).expanduser().resolve()
    work_path.mkdir(parents=True, exist_ok=True)
    source_dir = work_path / "busybox-src" / busybox_ref
    build_dir = work_path / "busybox-build" / normalized_arch / busybox_ref
    rootfs_dir = work_path / "busybox-rootfs" / normalized_arch / busybox_ref

    requested_output = (
        cached_initrd
        if output_path is None
        else Path(output_path).expanduser().resolve()
    )

    if not force_rebuild and requested_output.exists():
        return requested_output
    if not force_rebuild and cached_initrd.exists():
        if requested_output != cached_initrd:
            requested_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached_initrd, requested_output)
        return requested_output

    if force_rebuild:
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(rootfs_dir, ignore_errors=True)

    _ensure_busybox_source(source_dir, busybox_ref)
    build_dir.mkdir(parents=True, exist_ok=True)
    rootfs_dir.mkdir(parents=True, exist_ok=True)

    _ensure_docker_builder_image()
    _build_busybox_in_docker(
        arch=normalized_arch,
        source_dir=source_dir,
        build_dir=build_dir,
        rootfs_dir=rootfs_dir,
    )

    for relative in ("proc", "sys", "dev", "run", "tmp", "root"):
        (rootfs_dir / relative).mkdir(parents=True, exist_ok=True)
    _write_init_script(rootfs_dir)

    pack_initrd(rootfs_dir, cached_initrd, compress=True)
    if requested_output != cached_initrd:
        requested_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached_initrd, requested_output)
    return requested_output
