from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ._errors import CommandExecutionError, TestvmError

_GZIP_MAGIC = b"\x1f\x8b"


def _check_existing_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def pack_initrd(
    source_dir: str | Path,
    output_path: str | Path,
    *,
    compress: bool = True,
) -> Path:
    source_path = Path(source_dir).resolve()
    output = Path(output_path).resolve()

    if not source_path.is_dir():
        raise TestvmError(f"Rootfs source directory does not exist: {source_path}")

    _check_existing_output(output)

    find_proc = subprocess.Popen(
        ["find", ".", "-mindepth", "1", "-print0"],
        cwd=source_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cpio_proc = subprocess.Popen(
        ["cpio", "--null", "--create", "--format=newc"],
        cwd=source_path,
        stdin=find_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert find_proc.stdout is not None
    find_proc.stdout.close()

    gzip_proc: subprocess.Popen[bytes] | None = None
    try:
        if compress:
            with output.open("wb") as handle:
                gzip_proc = subprocess.Popen(
                    ["gzip", "-n", "-c"],
                    stdin=cpio_proc.stdout,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                )
                assert cpio_proc.stdout is not None
                cpio_proc.stdout.close()
                gzip_stderr = gzip_proc.communicate()[1]
            gzip_code = gzip_proc.returncode
        else:
            assert cpio_proc.stdout is not None
            with output.open("wb") as handle:
                shutil.copyfileobj(cpio_proc.stdout, handle)
            cpio_proc.stdout.close()
            gzip_code = 0
            gzip_stderr = b""

        find_stderr = b"" if find_proc.stderr is None else find_proc.stderr.read()
        cpio_stderr = b"" if cpio_proc.stderr is None else cpio_proc.stderr.read()
        find_code = find_proc.wait()
        cpio_code = cpio_proc.wait()
    finally:
        if find_proc.stderr is not None:
            find_proc.stderr.close()
        if cpio_proc.stderr is not None:
            cpio_proc.stderr.close()
        if gzip_proc is not None and gzip_proc.stderr is not None:
            gzip_proc.stderr.close()

    if find_code != 0:
        raise CommandExecutionError(find_stderr.decode().strip() or "find failed")
    if cpio_code != 0:
        raise CommandExecutionError(cpio_stderr.decode().strip() or "cpio failed")
    if gzip_code != 0:
        raise CommandExecutionError(gzip_stderr.decode().strip() or "gzip failed")

    return output


def unpack_initrd(initrd_path: str | Path, output_dir: str | Path) -> Path:
    source = Path(initrd_path).resolve()
    destination = Path(output_dir).resolve()

    if not source.is_file():
        raise TestvmError(f"Initrd does not exist: {source}")

    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise TestvmError(f"Output directory must be empty: {destination}")

    with source.open("rb") as handle:
        header = handle.read(2)

    extract_cmd = [
        "cpio",
        "--extract",
        "--make-directories",
        "--preserve-modification-time",
        "--no-absolute-filenames",
    ]

    if header == _GZIP_MAGIC:
        gzip_proc = subprocess.Popen(
            ["gzip", "-dc", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cpio_proc = subprocess.Popen(
            extract_cmd,
            cwd=destination,
            stdin=gzip_proc.stdout,
            stderr=subprocess.PIPE,
        )
        assert gzip_proc.stdout is not None
        gzip_proc.stdout.close()
        try:
            cpio_stderr = b"" if cpio_proc.stderr is None else cpio_proc.stderr.read()
            gzip_stderr = b"" if gzip_proc.stderr is None else gzip_proc.stderr.read()
            cpio_code = cpio_proc.wait()
            gzip_code = gzip_proc.wait()
        finally:
            if cpio_proc.stderr is not None:
                cpio_proc.stderr.close()
            if gzip_proc.stderr is not None:
                gzip_proc.stderr.close()
        if gzip_code != 0:
            raise CommandExecutionError(gzip_stderr.decode().strip() or "gzip failed")
        if cpio_code != 0:
            raise CommandExecutionError(cpio_stderr.decode().strip() or "cpio failed")
    else:
        with source.open("rb") as handle:
            result = subprocess.run(
                extract_cmd,
                cwd=destination,
                stdin=handle,
                capture_output=True,
                check=False,
            )
        if result.returncode != 0:
            raise CommandExecutionError(result.stderr.decode().strip() or "cpio failed")

    return destination
