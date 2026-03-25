from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testvm import CommandExecutionError, DATA_DIR_ENV_VAR, TestvmError
from testvm._arch import detect_kernel_arch
from testvm._busybox import (
    DEFAULT_BUSYBOX_REF,
    DOCKER_BUILD_DIR,
    DOCKER_IMAGE_TAG,
    DOCKER_ROOTFS_DIR,
    DOCKER_SOURCE_DIR,
    _run_docker_checked,
    build_default_initrd,
)
from testvm._initrd import pack_initrd, unpack_initrd
from testvm._paths import get_data_dir
from testvm._qemu import _build_qemu_command, run_vm


def _make_elf(path: Path, machine: int) -> None:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    header[18:20] = machine.to_bytes(2, "little")
    path.write_bytes(header)


class DataDirTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {DATA_DIR_ENV_VAR: temp_dir}, clear=False):
                self.assertEqual(get_data_dir(), Path(temp_dir))

    def test_linux_default_uses_local_var(self) -> None:
        fake_home = Path("/tmp/testvm-home")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("testvm._paths.platform.system", return_value="Linux"):
                with mock.patch("testvm._paths.Path.home", return_value=fake_home):
                    self.assertEqual(
                        get_data_dir(create=False),
                        fake_home / ".local" / "var" / "testvm",
                    )


class InitrdTests(unittest.TestCase):
    def test_pack_unpack_roundtrip_preserves_basic_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            rootfs = temp_path / "rootfs"
            rootfs.mkdir()
            script = rootfs / "hello.sh"
            script.write_text("#!/bin/sh\necho hi\n")
            script.chmod(0o755)
            (rootfs / "config.txt").write_text("value=1\n")
            (rootfs / "subdir").mkdir()
            (rootfs / "subdir" / "nested.txt").write_text("nested\n")
            (rootfs / "hello-link").symlink_to("hello.sh")

            initrd = temp_path / "rootfs.cpio.gz"
            unpacked = temp_path / "unpacked"

            pack_initrd(rootfs, initrd)
            unpack_initrd(initrd, unpacked)

            self.assertEqual((unpacked / "config.txt").read_text(), "value=1\n")
            self.assertEqual((unpacked / "subdir" / "nested.txt").read_text(), "nested\n")
            self.assertTrue((unpacked / "hello-link").is_symlink())
            self.assertEqual(os.readlink(unpacked / "hello-link"), "hello.sh")
            mode = stat.S_IMODE((unpacked / "hello.sh").stat().st_mode)
            self.assertEqual(mode, 0o755)

    def test_unpack_requires_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            rootfs = temp_path / "rootfs"
            rootfs.mkdir()
            (rootfs / "file.txt").write_text("data\n")
            initrd = temp_path / "rootfs.cpio.gz"
            pack_initrd(rootfs, initrd)

            output_dir = temp_path / "output"
            output_dir.mkdir()
            (output_dir / "preexisting.txt").write_text("nope\n")

            with self.assertRaises(TestvmError):
                unpack_initrd(initrd, output_dir)


class DetectArchTests(unittest.TestCase):
    def test_detect_x86_64(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vmlinux"
            _make_elf(path, 62)
            self.assertEqual(detect_kernel_arch(path), "x86_64")

    def test_detect_aarch64(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Image"
            _make_elf(path, 183)
            self.assertEqual(detect_kernel_arch(path), "aarch64")


class BusyBoxBuildTests(unittest.TestCase):
    def test_build_default_initrd_reuses_cached_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            cached = (
                temp_path
                / "cache"
                / "busybox"
                / "x86_64"
                / DEFAULT_BUSYBOX_REF
                / "initrd.cpio.gz"
            )
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached")
            output = temp_path / "out" / "initrd.cpio.gz"

            with mock.patch("testvm._busybox.get_data_dir", return_value=temp_path / "cache"):
                with mock.patch("testvm._busybox.get_host_arch", return_value="x86_64"):
                    path = build_default_initrd(arch="x86_64", output_path=output)

            self.assertEqual(path, output)
            self.assertEqual(output.read_bytes(), b"cached")

    def test_build_default_initrd_refuses_non_host_arch(self) -> None:
        with mock.patch("testvm._busybox.get_host_arch", return_value="x86_64"):
            with self.assertRaises(TestvmError):
                build_default_initrd(arch="aarch64")

    def test_build_default_initrd_uses_docker_build_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            data_dir = temp_path / "data"
            workdir = temp_path / "work"
            source_dir = workdir / "busybox-src" / DEFAULT_BUSYBOX_REF
            source_dir.mkdir(parents=True)
            commands: list[list[str]] = []

            def fake_docker(command: list[str]) -> None:
                commands.append(command)

            def fake_pack(rootfs_dir: str | Path, output_path: str | Path, *, compress: bool = True) -> Path:
                output = Path(output_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"initrd")
                return output

            with mock.patch("testvm._busybox.get_data_dir", return_value=data_dir):
                with mock.patch("testvm._busybox.get_host_arch", return_value="x86_64"):
                    with mock.patch("testvm._busybox._ensure_busybox_source") as source_mock:
                        with mock.patch("testvm._busybox._run_docker_checked", side_effect=fake_docker):
                            with mock.patch("testvm._busybox.pack_initrd", side_effect=fake_pack):
                                path = build_default_initrd(
                                    arch="x86_64",
                                    workdir=workdir,
                                    busybox_ref=DEFAULT_BUSYBOX_REF,
                                )
            source_mock.assert_called_once()
            self.assertEqual(path.read_bytes(), b"initrd")
            self.assertEqual(len(commands), 2)

            build_cmd, run_cmd = commands
            self.assertEqual(build_cmd[:4], ["docker", "build", "--tag", DOCKER_IMAGE_TAG])
            self.assertEqual(run_cmd[:3], ["docker", "run", "--rm"])
            self.assertIn(DOCKER_IMAGE_TAG, run_cmd)
            self.assertIn("silentoldconfig", run_cmd[-1])
            self.assertNotIn("olddefconfig", run_cmd[-1])
            self.assertIn("CONFIG_TC=y", run_cmd[-1])
            self.assertIn("# CONFIG_TC is not set", run_cmd[-1])

            build_mount = next(item for item in run_cmd if f"dst={DOCKER_BUILD_DIR}" in item)
            rootfs_mount = next(item for item in run_cmd if f"dst={DOCKER_ROOTFS_DIR}" in item)
            source_mount = next(item for item in run_cmd if f"dst={DOCKER_SOURCE_DIR}" in item)
            self.assertIn(f"src={source_dir}", source_mount)
            self.assertIn(f"src={workdir / 'busybox-build' / 'x86_64' / DEFAULT_BUSYBOX_REF}", build_mount)
            self.assertIn(f"src={workdir / 'busybox-rootfs' / 'x86_64' / DEFAULT_BUSYBOX_REF}", rootfs_mount)

    def test_run_docker_checked_rewords_permission_error(self) -> None:
        with mock.patch(
            "testvm._busybox._run_checked",
            side_effect=CommandExecutionError(
                "docker run: permission denied while trying to connect to the docker API"
            ),
        ):
            with self.assertRaisesRegex(TestvmError, "Docker daemon access failed"):
                _run_docker_checked(["docker", "run"])


class RunVmTests(unittest.TestCase):
    def test_build_qemu_command_for_x86_64(self) -> None:
        command = _build_qemu_command(
            kernel=Path("/tmp/vmlinux"),
            arch="x86_64",
            initrd=Path("/tmp/initrd.cpio.gz"),
            gdb_port=1234,
            memory="1G",
            smp=2,
            append=["panic=-1"],
            qemu_arg=["-no-reboot"],
        )
        self.assertIn("qemu-system-x86_64", command[0])
        self.assertIn("-initrd", command)
        self.assertIn("tcp::1234", command)
        self.assertEqual(command[-1], "-no-reboot")
        self.assertIn("console=ttyS0 rdinit=/init panic=-1", command)

    def test_run_vm_autobuilds_initrd_on_host_arch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            kernel = temp_path / "vmlinux"
            initrd = temp_path / "initrd.cpio.gz"
            _make_elf(kernel, 62)
            initrd.write_bytes(b"initrd")

            with mock.patch("testvm._qemu.build_default_initrd", return_value=initrd) as build_mock:
                with mock.patch("testvm._qemu.subprocess.run") as run_mock:
                    run_mock.return_value.returncode = 0
                    exit_code = run_vm(kernel=kernel)

            self.assertEqual(exit_code, 0)
            build_mock.assert_called_once()
            run_mock.assert_called_once()

    def test_run_vm_requires_initrd_for_non_host_arch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "Image"
            _make_elf(kernel, 183)
            with mock.patch("testvm._qemu.get_host_arch", return_value="x86_64"):
                with self.assertRaises(TestvmError):
                    run_vm(kernel=kernel)
