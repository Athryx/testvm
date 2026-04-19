from __future__ import annotations

from enum import StrEnum
import platform
import subprocess
import struct
from pathlib import Path

from ._errors import TestvmError, UnsupportedArchitectureError


class Architecture(StrEnum):
    X86_64 = "x86_64"
    ARM = "arm"
    AARCH64 = "aarch64"


_HOST_ARCHES = {
    "x86_64": Architecture.X86_64,
    "amd64": Architecture.X86_64,
    "arm": Architecture.ARM,
    "armv7": Architecture.ARM,
    "armv7l": Architecture.ARM,
    "armhf": Architecture.ARM,
    "aarch64": Architecture.AARCH64,
    "arm64": Architecture.AARCH64,
}

_ELF_MACHINE_ARCHES = {
    62: Architecture.X86_64,
    40: Architecture.ARM,
    183: Architecture.AARCH64,
}


_MAGIC_DESCRIPTION_ARCHES = (
    ("arm64", Architecture.AARCH64),
    ("aarch64", Architecture.AARCH64),
    ("arm 64", Architecture.AARCH64),
    ("arm", Architecture.ARM),
    ("x86-64", Architecture.X86_64),
    ("x86_64", Architecture.X86_64),
    ("amd64", Architecture.X86_64),
)


def normalize_arch(arch: str | Architecture) -> Architecture:
    if isinstance(arch, Architecture):
        return arch
    normalized = _HOST_ARCHES.get(arch.lower())
    if normalized is None:
        raise UnsupportedArchitectureError(f"Unsupported architecture: {arch}")
    return normalized


def get_host_arch() -> Architecture:
    return normalize_arch(platform.machine())


def _detect_elf_arch(path: Path, header: bytes) -> Architecture | None:
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    elf_data = header[5]
    if elf_data == 1:
        machine = struct.unpack_from("<H", header, 18)[0]
    elif elf_data == 2:
        machine = struct.unpack_from(">H", header, 18)[0]
    else:
        raise TestvmError(f"{path} has an unknown ELF data encoding")

    arch = _ELF_MACHINE_ARCHES.get(machine)
    if arch is None:
        raise UnsupportedArchitectureError(
            f"Unsupported ELF machine type {machine} in {path}"
        )
    return arch


def _get_file_description(path: Path) -> str:
    try:
        result = subprocess.run(
            ["file", "-b", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TestvmError(
            f"{path} is not an ELF file and the file command is unavailable; "
            "install file/libmagic or pass --arch explicitly"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        raise TestvmError(f"Failed to inspect {path} with the file command{detail}")

    description = result.stdout.strip()
    if not description:
        raise TestvmError(f"file returned no type information for {path}")
    return description


def _detect_file_arch(path: Path) -> Architecture:
    description = _get_file_description(path)
    normalized = description.lower()

    for marker, arch in _MAGIC_DESCRIPTION_ARCHES:
        if marker in normalized:
            return arch

    if "linux kernel" in normalized and "x86" in normalized:
        return Architecture.X86_64

    raise UnsupportedArchitectureError(
        f"Could not detect architecture for kernel image {path}: {description}"
    )


def detect_kernel_arch(vmlinux_path: str | Path) -> Architecture:
    path = Path(vmlinux_path)
    with path.open("rb") as handle:
        header = handle.read(64)

    arch = _detect_elf_arch(path, header)
    if arch is not None:
        return arch
    return _detect_file_arch(path)
