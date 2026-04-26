from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from testvm.cli import app

runner = CliRunner()


class CliTests(unittest.TestCase):
    def test_pack_command_calls_library_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            rootfs = temp_path / "rootfs"
            rootfs.mkdir()
            output = temp_path / "initrd.cpio.gz"

            with mock.patch("testvm.cli.pack_initrd", return_value=output) as pack_mock:
                result = runner.invoke(app, ["initrd", "pack", str(rootfs), str(output)])

            self.assertEqual(result.exit_code, 0)
            pack_mock.assert_called_once_with(rootfs, output)
            self.assertIn(str(output), result.stdout)

    def test_pack_ext4_command_calls_library_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_dir = temp_path / "shared"
            source_dir.mkdir()
            output = temp_path / "shared.img"

            with mock.patch("testvm.cli.pack_ext4_image", return_value=output) as pack_mock:
                result = runner.invoke(
                    app,
                    ["ext4", "pack", str(source_dir), str(output), "--size", "128M"],
                )

            self.assertEqual(result.exit_code, 0)
            pack_mock.assert_called_once_with(source_dir, output, size="128M")
            self.assertIn(str(output), result.stdout)

    def test_unpack_ext4_command_calls_library_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            image = temp_path / "shared.img"
            image.write_bytes(b"unused")
            output = temp_path / "shared-out"

            with mock.patch("testvm.cli.unpack_ext4_image", return_value=output) as unpack_mock:
                result = runner.invoke(app, ["ext4", "unpack", str(image), str(output)])

            self.assertEqual(result.exit_code, 0)
            unpack_mock.assert_called_once_with(image, output)
            self.assertIn(str(output), result.stdout)

    def test_run_command_calls_library_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "vmlinux"
            kernel.write_bytes(b"not-used")
            shared = Path(temp_dir) / "shared"
            shared.mkdir()
            autorun = shared / "run.sh"
            autorun.write_text("#!/bin/sh\n")
            module_rootfs = Path(temp_dir) / "module-rootfs"
            module_rootfs.mkdir()

            with mock.patch("testvm.cli.run_vm", return_value=7) as run_mock:
                result = runner.invoke(
                    app,
                    [
                        "run",
                        str(kernel),
                        "--arch",
                        "x86_64",
                        "--memory",
                        "1G",
                        "--append",
                        "panic=-1",
                        "--nokaslr",
                        "--qemu-arg",
                        "-no-reboot",
                        "--network",
                        "user",
                        "--hostfwd",
                        "10022:22",
                        "--network-ip",
                        "192.168.50.10/24",
                        "--network-gateway",
                        "192.168.50.1",
                        "--network-dns",
                        "1.1.1.1",
                        "--network-host-ip",
                        "192.168.50.1",
                        "--module-initrd",
                        str(module_rootfs),
                        "--share-dir",
                        str(shared),
                        "--share-mode",
                        "ext4",
                        "--autorun",
                        str(autorun),
                        "--sync-share-back",
                    ],
                )

            self.assertEqual(result.exit_code, 7)
            run_mock.assert_called_once()
            self.assertEqual(run_mock.call_args.kwargs["module_initrd"], module_rootfs)
            self.assertTrue(run_mock.call_args.kwargs["nokaslr"])
            self.assertEqual(run_mock.call_args.kwargs["share_mode"], "ext4")
            self.assertEqual(run_mock.call_args.kwargs["autorun_path"], autorun)
            self.assertEqual(run_mock.call_args.kwargs["network"], "user")
            self.assertEqual(run_mock.call_args.kwargs["hostfwd"], ["10022:22"])
            self.assertEqual(
                run_mock.call_args.kwargs["network_ip"], "192.168.50.10/24"
            )
            self.assertEqual(run_mock.call_args.kwargs["network_gateway"], "192.168.50.1")
            self.assertEqual(run_mock.call_args.kwargs["network_dns"], ["1.1.1.1"])
            self.assertEqual(run_mock.call_args.kwargs["network_host_ip"], "192.168.50.1")

    def test_run_command_accepts_arm_arch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "zImage"
            kernel.write_bytes(b"raw")

            with mock.patch("testvm.cli.run_vm", return_value=0) as run_mock:
                result = runner.invoke(app, ["run", str(kernel), "--arch", "arm"])

            self.assertEqual(result.exit_code, 0)
            run_mock.assert_called_once()
