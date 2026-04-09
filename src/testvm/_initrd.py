from __future__ import annotations

import tempfile
import shutil
import subprocess
from pathlib import Path

from ._errors import CommandExecutionError, TestvmError

_GZIP_MAGIC = b"\x1f\x8b"
_LZ4_MAGIC = b"\x04\x22\x4d\x18"
_LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"
_ORIGINAL_INIT_RELATIVE_PATH = Path(".testvm") / "original-init"
_SHARE_ROOTFS_RELATIVE_PATH = Path("mnt") / "testvm-share"


def _check_existing_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _wait_for_extract_pipeline(
    *,
    decompress_proc: subprocess.Popen[bytes],
    cpio_proc: subprocess.Popen[bytes],
    decompressor_name: str,
) -> None:
    assert decompress_proc.stdout is not None
    decompress_proc.stdout.close()
    try:
        cpio_stderr = b"" if cpio_proc.stderr is None else cpio_proc.stderr.read()
        decompress_stderr = (
            b"" if decompress_proc.stderr is None else decompress_proc.stderr.read()
        )
        cpio_code = cpio_proc.wait()
        decompress_code = decompress_proc.wait()
    finally:
        if cpio_proc.stderr is not None:
            cpio_proc.stderr.close()
        if decompress_proc.stderr is not None:
            decompress_proc.stderr.close()

    if decompress_code != 0:
        raise CommandExecutionError(
            decompress_stderr.decode().strip() or f"{decompressor_name} failed"
        )
    if cpio_code != 0:
        raise CommandExecutionError(cpio_stderr.decode().strip() or "cpio failed")


def _remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        if destination.exists() or destination.is_symlink():
            _remove_existing(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source.readlink())
        return

    if source.is_dir():
        if destination.exists() and not destination.is_dir():
            _remove_existing(destination)
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            dirs_exist_ok=True,
            copy_function=shutil.copy2,
        )
        return

    if destination.exists():
        _remove_existing(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


def _copy_tree_contents(source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    for child in source_dir.iterdir():
        _copy_entry(child, destination_dir / child.name)


def _populate_rootfs_tree(source_path: Path, destination_dir: Path) -> None:
    if source_path.is_dir():
        _copy_tree_contents(source_path, destination_dir)
        return
    if source_path.is_file():
        unpack_initrd(source_path, destination_dir)
        return
    raise TestvmError(f"Initrd overlay path does not exist: {source_path}")


def _populate_shared_directory(source_dir: Path, rootfs_dir: Path) -> None:
    if not source_dir.is_dir():
        raise TestvmError(f"Shared directory does not exist: {source_dir}")

    share_root = rootfs_dir / _SHARE_ROOTFS_RELATIVE_PATH
    share_root.mkdir(parents=True, exist_ok=True)
    _copy_tree_contents(source_dir, share_root)


def _write_module_init_wrapper(rootfs_dir: Path) -> None:
    init_path = rootfs_dir / "init"
    if not (init_path.exists() or init_path.is_symlink()):
        raise TestvmError(f"Base initrd does not contain /init: {rootfs_dir}")

    shell_path = rootfs_dir / "bin" / "sh"
    if not (shell_path.exists() or shell_path.is_symlink()):
        raise TestvmError(
            f"Merged initrd does not contain /bin/sh required for the module wrapper: {rootfs_dir}"
        )

    original_init_path = rootfs_dir / _ORIGINAL_INIT_RELATIVE_PATH
    original_init_path.parent.mkdir(parents=True, exist_ok=True)
    if original_init_path.exists() or original_init_path.is_symlink():
        _remove_existing(original_init_path)
    init_path.rename(original_init_path)

    wrapper = """#!/bin/sh
set -eu

mkdir -p /dev /proc /sys /run /tmp /root
[ -c /dev/console ] || mknod -m 600 /dev/console c 5 1 2>/dev/null || true

testvm_mounted_proc=0
testvm_mounted_sys=0
testvm_mounted_dev=0

if mount -t proc proc /proc 2>/dev/null; then
    testvm_mounted_proc=1
fi
if mount -t sysfs sysfs /sys 2>/dev/null; then
    testvm_mounted_sys=1
fi
if mount -t devtmpfs devtmpfs /dev 2>/dev/null; then
    testvm_mounted_dev=1
fi

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

run_testvm_modprobe() {
    if command -v modprobe >/dev/null 2>&1; then
        modprobe "$1"
    elif command -v busybox >/dev/null 2>&1; then
        busybox modprobe "$1"
    else
        return 127
    fi
}

load_testvm_modules() {
    kernel_release=$(uname -r 2>/dev/null || echo "")
    modules_load=""

    if [ -n "$kernel_release" ] && [ -f "/lib/modules/$kernel_release/modules.load" ]; then
        modules_load="/lib/modules/$kernel_release/modules.load"
    else
        for candidate in /lib/modules/*/modules.load; do
            if [ -f "$candidate" ]; then
                modules_load="$candidate"
                break
            fi
        done
    fi

    if [ -z "$modules_load" ] || [ ! -f "$modules_load" ]; then
        return 0
    fi

    if ! command -v modprobe >/dev/null 2>&1 && ! command -v busybox >/dev/null 2>&1; then
        echo "warning: no modprobe applet available for $modules_load"
        return 0
    fi

    while IFS= read -r module_entry || [ -n "$module_entry" ]; do
        case "$module_entry" in
            ""|[#]*)
                continue
                ;;
        esac

        module_name=$module_entry
        module_name=${module_name##*/}
        case "$module_name" in
            *.ko.gz)
                module_name=${module_name%.ko.gz}
                ;;
            *.ko.xz)
                module_name=${module_name%.ko.xz}
                ;;
            *.ko.zst)
                module_name=${module_name%.ko.zst}
                ;;
            *.ko)
                module_name=${module_name%.ko}
                ;;
        esac

        if [ -z "$module_name" ]; then
            continue
        fi

        set +e
        run_testvm_modprobe "$module_name"
        status=$?
        set -e
        if [ "$status" -ne 0 ]; then
            echo "warning: modprobe failed for $module_name from $module_entry ($status)"
        fi
    done < "$modules_load"
}

load_testvm_modules

if [ "$testvm_mounted_dev" = "1" ]; then
    umount /dev 2>/dev/null || true
fi
if [ "$testvm_mounted_sys" = "1" ]; then
    umount /sys 2>/dev/null || true
fi
if [ "$testvm_mounted_proc" = "1" ]; then
    umount /proc 2>/dev/null || true
fi

exec /.testvm/original-init "$@"
"""
    init_path.write_text(wrapper)
    init_path.chmod(0o755)


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
        header = handle.read(4)

    extract_cmd = [
        "cpio",
        "--extract",
        "--make-directories",
        "--preserve-modification-time",
        "--no-absolute-filenames",
    ]

    if header.startswith(_GZIP_MAGIC):
        try:
            gzip_proc = subprocess.Popen(
                ["gzip", "-dc", str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CommandExecutionError("Command not found: gzip") from exc
        cpio_proc = subprocess.Popen(
            extract_cmd,
            cwd=destination,
            stdin=gzip_proc.stdout,
            stderr=subprocess.PIPE,
        )
        _wait_for_extract_pipeline(
            decompress_proc=gzip_proc,
            cpio_proc=cpio_proc,
            decompressor_name="gzip",
        )
    elif header in (_LZ4_MAGIC, _LZ4_LEGACY_MAGIC):
        try:
            lz4_proc = subprocess.Popen(
                ["lz4", "-dc", str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CommandExecutionError("Command not found: lz4") from exc
        cpio_proc = subprocess.Popen(
            extract_cmd,
            cwd=destination,
            stdin=lz4_proc.stdout,
            stderr=subprocess.PIPE,
        )
        _wait_for_extract_pipeline(
            decompress_proc=lz4_proc,
            cpio_proc=cpio_proc,
            decompressor_name="lz4",
        )
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


def build_merged_initrd(
    base_initrd: str | Path,
    module_overlay: str | Path,
    *,
    output_path: str | Path | None = None,
) -> Path:
    return _build_composed_initrd(
        base_initrd,
        output_path=output_path,
        module_overlay=module_overlay,
    )


def _build_composed_initrd(
    base_initrd: str | Path,
    *,
    output_path: str | Path | None = None,
    module_overlay: str | Path | None = None,
    shared_dir: str | Path | None = None,
) -> Path:
    base_path = Path(base_initrd).expanduser().resolve()
    if not base_path.is_file():
        raise TestvmError(f"Base initrd does not exist: {base_path}")

    overlay_path = (
        None
        if module_overlay is None
        else Path(module_overlay).expanduser().resolve()
    )
    share_path = (
        None if shared_dir is None else Path(shared_dir).expanduser().resolve()
    )

    requested_output: Path
    temp_output_handle: tempfile.NamedTemporaryFile[bytes] | None = None
    if output_path is None:
        temp_output_handle = tempfile.NamedTemporaryFile(
            prefix="testvm-merged-initrd-",
            suffix=".cpio.gz",
            delete=False,
        )
        temp_output_handle.close()
        requested_output = Path(temp_output_handle.name).resolve()
    else:
        requested_output = Path(output_path).expanduser().resolve()

    _check_existing_output(requested_output)

    with tempfile.TemporaryDirectory(prefix="testvm-merge-initrd-") as temp_dir:
        temp_root = Path(temp_dir)
        base_rootfs = temp_root / "base-rootfs"
        overlay_rootfs = temp_root / "overlay-rootfs"

        unpack_initrd(base_path, base_rootfs)
        if overlay_path is not None:
            _populate_rootfs_tree(overlay_path, overlay_rootfs)
            _copy_tree_contents(overlay_rootfs, base_rootfs)
        if share_path is not None:
            _populate_shared_directory(share_path, base_rootfs)
        if overlay_path is not None:
            _write_module_init_wrapper(base_rootfs)
        pack_initrd(base_rootfs, requested_output, compress=True)

    return requested_output
