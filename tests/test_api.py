from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testvm import CommandExecutionError, DATA_DIR_ENV_VAR, TestvmError
from testvm._arch import Architecture, detect_kernel_arch, normalize_arch
from testvm._busybox import (
    DEFAULT_BUSYBOX_REF,
    DOCKER_BUILD_DIR,
    DOCKER_IMAGE_TAG,
    DOCKER_ROOTFS_DIR,
    DOCKER_SOURCE_DIR,
    _run_docker_checked,
    _write_init_script,
    build_default_initrd,
)
from testvm._ext4 import pack_ext4_image, unpack_ext4_image
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


class Ext4Tests(unittest.TestCase):
    def test_pack_unpack_ext4_roundtrip_preserves_basic_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            shared = temp_path / "shared"
            shared.mkdir()
            script = shared / "hello.sh"
            script.write_text("#!/bin/sh\necho hi\n")
            script.chmod(0o755)
            (shared / "config.txt").write_text("value=1\n")
            (shared / "subdir").mkdir()
            (shared / "subdir" / "nested.txt").write_text("nested\n")
            (shared / "hello-link").symlink_to("hello.sh")

            image = temp_path / "shared.img"
            unpacked = temp_path / "shared-out"

            pack_ext4_image(shared, image)
            unpack_ext4_image(image, unpacked)

            self.assertEqual((unpacked / "config.txt").read_text(), "value=1\n")
            self.assertEqual((unpacked / "subdir" / "nested.txt").read_text(), "nested\n")
            self.assertTrue((unpacked / "hello-link").is_symlink())
            self.assertEqual(os.readlink(unpacked / "hello-link"), "hello.sh")
            mode = stat.S_IMODE((unpacked / "hello.sh").stat().st_mode)
            self.assertEqual(mode, 0o755)
            self.assertFalse((unpacked / "lost+found").exists())

    def test_unpack_ext4_requires_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            shared = temp_path / "shared"
            shared.mkdir()
            (shared / "file.txt").write_text("data\n")
            image = temp_path / "shared.img"
            pack_ext4_image(shared, image)

            output_dir = temp_path / "output"
            output_dir.mkdir()
            (output_dir / "preexisting.txt").write_text("nope\n")

            with self.assertRaises(TestvmError):
                unpack_ext4_image(image, output_dir)

    def test_pack_ext4_rejects_invalid_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            shared = temp_path / "shared"
            shared.mkdir()

            with self.assertRaisesRegex(TestvmError, "Invalid ext4 image size"):
                pack_ext4_image(shared, temp_path / "shared.img", size="abc")


class DetectArchTests(unittest.TestCase):
    def test_normalize_arm_aliases(self) -> None:
        self.assertEqual(normalize_arch("arm"), Architecture.ARM)
        self.assertEqual(normalize_arch("armv7"), Architecture.ARM)
        self.assertEqual(normalize_arch("armhf"), Architecture.ARM)

    def test_detect_x86_64(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vmlinux"
            _make_elf(path, 62)
            self.assertEqual(detect_kernel_arch(path), "x86_64")

    def test_detect_arm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "zImage.elf"
            _make_elf(path, 40)
            self.assertEqual(detect_kernel_arch(path), "arm")

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
                path = build_default_initrd(arch="x86_64", output_path=output)

            self.assertEqual(path, output)
            self.assertEqual(output.read_bytes(), b"cached")

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

    def test_build_default_initrd_uses_arm_cross_compile_vars(self) -> None:
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
                output.write_bytes(b"arm-initrd")
                return output

            with mock.patch("testvm._busybox.get_data_dir", return_value=data_dir):
                with mock.patch("testvm._busybox._ensure_busybox_source"):
                    with mock.patch("testvm._busybox._run_docker_checked", side_effect=fake_docker):
                        with mock.patch("testvm._busybox.pack_initrd", side_effect=fake_pack):
                            path = build_default_initrd(
                                arch="arm",
                                workdir=workdir,
                                busybox_ref=DEFAULT_BUSYBOX_REF,
                            )

            self.assertEqual(path.read_bytes(), b"arm-initrd")
            _, run_cmd = commands
            self.assertIn("ARCH=arm", run_cmd[-1])
            self.assertIn("CROSS_COMPILE=arm-linux-gnueabihf-", run_cmd[-1])
            self.assertIn("CONFIG_SHA1_HWACCEL", run_cmd[-1])
            self.assertIn("CONFIG_SHA256_HWACCEL", run_cmd[-1])
            self.assertIn(
                f"src={workdir / 'busybox-build' / 'arm' / DEFAULT_BUSYBOX_REF}",
                next(item for item in run_cmd if f"dst={DOCKER_BUILD_DIR}" in item),
            )

    def test_build_default_initrd_uses_aarch64_cross_compile_vars(self) -> None:
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
                output.write_bytes(b"aarch64-initrd")
                return output

            with mock.patch("testvm._busybox.get_data_dir", return_value=data_dir):
                with mock.patch("testvm._busybox._ensure_busybox_source"):
                    with mock.patch("testvm._busybox._run_docker_checked", side_effect=fake_docker):
                        with mock.patch("testvm._busybox.pack_initrd", side_effect=fake_pack):
                            path = build_default_initrd(
                                arch="aarch64",
                                workdir=workdir,
                                busybox_ref=DEFAULT_BUSYBOX_REF,
                            )

            self.assertEqual(path.read_bytes(), b"aarch64-initrd")
            _, run_cmd = commands
            self.assertIn("ARCH=arm64", run_cmd[-1])
            self.assertIn("CROSS_COMPILE=aarch64-linux-gnu-", run_cmd[-1])
            self.assertIn("CONFIG_SHA1_HWACCEL", run_cmd[-1])
            self.assertIn("CONFIG_SHA256_HWACCEL", run_cmd[-1])

    def test_run_docker_checked_rewords_permission_error(self) -> None:
        with mock.patch(
            "testvm._busybox._run_checked",
            side_effect=CommandExecutionError(
                "docker run: permission denied while trying to connect to the docker API"
            ),
        ):
            with self.assertRaisesRegex(TestvmError, "Docker daemon access failed"):
                _run_docker_checked(["docker", "run"])

    def test_write_init_script_contains_share_mount_and_autorun_logic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rootfs_dir = Path(temp_dir)
            _write_init_script(rootfs_dir)

            script = (rootfs_dir / "init").read_text()
            self.assertIn("testvm_share=1", script)
            self.assertIn("/mnt/testvm-share", script)
            self.assertIn("testvm_autorun=", script)
            self.assertIn('"$autorun_path"', script)


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

    def test_build_qemu_command_adds_share_drive_and_autorun(self) -> None:
        command = _build_qemu_command(
            kernel=Path("/tmp/vmlinux"),
            arch="x86_64",
            initrd=Path("/tmp/initrd.cpio.gz"),
            gdb_port=None,
            memory="1G",
            smp=2,
            append=[],
            qemu_arg=[],
            share_image=Path("/tmp/share.img"),
            autorun="/mnt/testvm-share/run.sh",
        )
        self.assertIn("-drive", command)
        self.assertIn("file=/tmp/share.img,format=raw,if=virtio", command)
        self.assertIn(
            "console=ttyS0 rdinit=/init testvm_share=1 testvm_autorun=/mnt/testvm-share/run.sh",
            command,
        )

    def test_build_qemu_command_for_arm(self) -> None:
        command = _build_qemu_command(
            kernel=Path("/tmp/zImage"),
            arch="arm",
            initrd=Path("/tmp/initrd.cpio.gz"),
            gdb_port=None,
            memory="256M",
            smp=1,
            append=["panic=-1"],
            qemu_arg=[],
        )
        self.assertIn("qemu-system-arm", command[0])
        self.assertIn("virt", command)
        self.assertIn("cortex-a15", command)
        self.assertIn("console=ttyAMA0 rdinit=/init panic=-1", command)

    def test_run_vm_autobuilds_initrd_for_detected_arch(self) -> None:
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

    def test_run_vm_packs_share_image_and_syncs_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            kernel = temp_path / "vmlinux"
            initrd = temp_path / "initrd.cpio.gz"
            shared = temp_path / "shared"
            shared.mkdir()
            (shared / "before.txt").write_text("before\n")
            _make_elf(kernel, 62)
            initrd.write_bytes(b"initrd")

            def fake_pack(source_dir: str | Path, output_path: str | Path, *, size: str | None = None) -> Path:
                self.assertEqual(Path(source_dir), shared)
                output = Path(output_path)
                output.write_bytes(b"ext4")
                return output

            def fake_unpack(image_path: str | Path, output_dir: str | Path) -> Path:
                output = Path(output_dir)
                output.mkdir(parents=True, exist_ok=True)
                (output / "after.txt").write_text("after\n")
                return output

            with mock.patch("testvm._qemu.build_default_initrd", return_value=initrd):
                with mock.patch("testvm._qemu.pack_ext4_image", side_effect=fake_pack) as pack_mock:
                    with mock.patch("testvm._qemu.unpack_ext4_image", side_effect=fake_unpack) as unpack_mock:
                        with mock.patch("testvm._qemu.subprocess.run") as run_mock:
                            run_mock.return_value.returncode = 0
                            exit_code = run_vm(
                                kernel=kernel,
                                share_dir=shared,
                                sync_share_back=True,
                                autorun="/mnt/testvm-share/run.sh",
                            )

            self.assertEqual(exit_code, 0)
            pack_mock.assert_called_once()
            unpack_mock.assert_called_once()
            self.assertFalse((shared / "before.txt").exists())
            self.assertEqual((shared / "after.txt").read_text(), "after\n")
            command = run_mock.call_args.args[0]
            self.assertIn("-drive", command)
            self.assertIn("testvm_share=1", " ".join(command))
            self.assertIn("testvm_autorun=/mnt/testvm-share/run.sh", " ".join(command))

    def test_run_vm_run_host_path_infers_share_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            kernel = temp_path / "vmlinux"
            initrd = temp_path / "initrd.cpio.gz"
            shared = temp_path / "shared"
            shared.mkdir()
            host_program = shared / "bin" / "tool.sh"
            host_program.parent.mkdir()
            host_program.write_text("#!/bin/sh\necho ok\n")
            _make_elf(kernel, 62)
            initrd.write_bytes(b"initrd")

            def fake_pack(source_dir: str | Path, output_path: str | Path, *, size: str | None = None) -> Path:
                self.assertEqual(Path(source_dir), host_program.parent)
                output = Path(output_path)
                output.write_bytes(b"ext4")
                return output

            with mock.patch("testvm._qemu.build_default_initrd", return_value=initrd):
                with mock.patch("testvm._qemu.pack_ext4_image", side_effect=fake_pack):
                    with mock.patch("testvm._qemu.subprocess.run") as run_mock:
                        run_mock.return_value.returncode = 0
                        exit_code = run_vm(kernel=kernel, run_host_path=host_program)

            self.assertEqual(exit_code, 0)
            command = run_mock.call_args.args[0]
            self.assertIn(
                "testvm_autorun=/mnt/testvm-share/tool.sh",
                " ".join(command),
            )

    def test_run_vm_rejects_run_host_path_outside_share_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            kernel = temp_path / "vmlinux"
            shared = temp_path / "shared"
            shared.mkdir()
            outsider = temp_path / "outsider.sh"
            outsider.write_text("#!/bin/sh\necho no\n")
            _make_elf(kernel, 62)

            with self.assertRaisesRegex(TestvmError, "inside the shared directory"):
                run_vm(kernel=kernel, share_dir=shared, run_host_path=outsider)

    def test_run_vm_requires_share_for_sync_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "vmlinux"
            _make_elf(kernel, 62)

            with self.assertRaisesRegex(TestvmError, "requires --share-dir"):
                run_vm(kernel=kernel, sync_share_back=True)

    def test_run_vm_rejects_whitespace_in_autorun_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "vmlinux"
            _make_elf(kernel, 62)

            with self.assertRaisesRegex(TestvmError, "may not contain whitespace"):
                run_vm(kernel=kernel, autorun="/mnt/testvm-share/run me")

    def test_run_vm_autobuilds_initrd_for_arm_on_x86_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "Image"
            initrd = Path(temp_dir) / "initrd.cpio.gz"
            _make_elf(kernel, 40)
            initrd.write_bytes(b"initrd")

            with mock.patch("testvm._qemu.build_default_initrd", return_value=initrd) as build_mock:
                with mock.patch("testvm._qemu.subprocess.run") as run_mock:
                    run_mock.return_value.returncode = 0
                    exit_code = run_vm(kernel=kernel)

            self.assertEqual(exit_code, 0)
            build_mock.assert_called_once_with(
                arch=Architecture.ARM,
                workdir=None,
                force_rebuild=False,
            )
            run_mock.assert_called_once()

    def test_run_vm_accepts_raw_arm_kernel_when_arch_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "zImage"
            initrd = Path(temp_dir) / "initrd.cpio.gz"
            kernel.write_bytes(b"not-elf")
            initrd.write_bytes(b"initrd")

            with mock.patch("testvm._qemu.build_default_initrd", return_value=initrd) as build_mock:
                with mock.patch("testvm._qemu.subprocess.run") as run_mock:
                    run_mock.return_value.returncode = 0
                    exit_code = run_vm(kernel=kernel, arch="arm")

            self.assertEqual(exit_code, 0)
            build_mock.assert_called_once_with(
                arch=Architecture.ARM,
                workdir=None,
                force_rebuild=False,
            )
            run_mock.assert_called_once()
