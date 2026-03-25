from __future__ import annotations

from enum import StrEnum
import platform
import struct
from pathlib import Path

from ._errors import TestvmError, UnsupportedArchitectureError


class Architecture(StrEnum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"


_HOST_ARCHES = {
    "x86_64": Architecture.X86_64,
    "amd64": Architecture.X86_64,
    "aarch64": Architecture.AARCH64,
    "arm64": Architecture.AARCH64,
}

_ELF_MACHINE_ARCHES = {
    62: Architecture.X86_64,
    183: Architecture.AARCH64,
}


def normalize_arch(arch: str | Architecture) -> Architecture:
    if isinstance(arch, Architecture):
        return arch
    normalized = _HOST_ARCHES.get(arch.lower())
    if normalized is None:
        raise UnsupportedArchitectureError(f"Unsupported architecture: {arch}")
    return normalized


def get_host_arch() -> Architecture:
    return normalize_arch(platform.machine())


def detect_kernel_arch(vmlinux_path: str | Path) -> Architecture:
    path = Path(vmlinux_path)
    with path.open("rb") as handle:
        header = handle.read(64)

    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise TestvmError(f"{path} is not an ELF file")

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
