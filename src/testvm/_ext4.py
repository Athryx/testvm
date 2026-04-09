from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ._errors import CommandExecutionError, TestvmError

_SIZE_RE = re.compile(r"^(?P<value>\d+)(?P<suffix>[KMGTP]?)(?:B)?$", re.IGNORECASE)
_SIZE_SUFFIXES = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}
_MIN_IMAGE_SIZE = 64 * 1024 * 1024
_AUTO_IMAGE_SLACK = 16 * 1024 * 1024
_AUTO_IMAGE_ALIGNMENT = 1024 * 1024


def _mkdir_for_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _run_checked(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandExecutionError(f"Command not found: {command[0]}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise CommandExecutionError(f"{' '.join(command)}: {message}")
    return result


def _round_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


def _estimate_tree_bytes(source_dir: Path) -> int:
    total = 0
    for current_root, dir_names, file_names in os.walk(source_dir, followlinks=False):
        root_path = Path(current_root)
        total += 4096
        for dir_name in dir_names:
            total += max(4096, (root_path / dir_name).lstat().st_size)
        for file_name in file_names:
            total += max(4096, (root_path / file_name).lstat().st_size)
    return total


def _parse_size_bytes(size: str | None, *, source_dir: Path) -> int:
    if size is None:
        estimated = _estimate_tree_bytes(source_dir)
        return max(
            _MIN_IMAGE_SIZE,
            _round_up((estimated * 2) + _AUTO_IMAGE_SLACK, _AUTO_IMAGE_ALIGNMENT),
        )

    match = _SIZE_RE.fullmatch(size.strip())
    if match is None:
        raise TestvmError(f"Invalid ext4 image size: {size}")
    multiplier = _SIZE_SUFFIXES[match.group("suffix").upper()]
    size_bytes = int(match.group("value")) * multiplier
    if size_bytes <= 0:
        raise TestvmError(f"Invalid ext4 image size: {size}")
    return size_bytes


def pack_ext4_image(
    source_dir: str | Path,
    output_image: str | Path,
    *,
    size: str | None = None,
) -> Path:
    source_path = Path(source_dir).expanduser().resolve()
    output_path = Path(output_image).expanduser().resolve()

    if not source_path.is_dir():
        raise TestvmError(f"Ext4 source directory does not exist: {source_path}")

    size_bytes = _parse_size_bytes(size, source_dir=source_path)
    _mkdir_for_output(output_path)
    with output_path.open("wb") as handle:
        handle.truncate(size_bytes)

    _run_checked(["mkfs.ext4", "-d", str(source_path), "-F", str(output_path)])
    return output_path


def unpack_ext4_image(image_path: str | Path, output_dir: str | Path) -> Path:
    source_path = Path(image_path).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()

    if not source_path.is_file():
        raise TestvmError(f"Ext4 image does not exist: {source_path}")

    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise TestvmError(f"Output directory must be empty: {destination}")

    _run_checked(["debugfs", "-R", "rdump / .", str(source_path)], cwd=destination)

    lost_found = destination / "lost+found"
    if lost_found.exists():
        if lost_found.is_dir() and not lost_found.is_symlink():
            shutil.rmtree(lost_found)
        else:
            lost_found.unlink()
    return destination
