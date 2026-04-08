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

    def test_run_command_calls_library_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "vmlinux"
            kernel.write_bytes(b"not-used")

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
                        "--qemu-arg",
                        "-no-reboot",
                    ],
                )

            self.assertEqual(result.exit_code, 7)
            run_mock.assert_called_once()

    def test_run_command_accepts_arm_arch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kernel = Path(temp_dir) / "zImage"
            kernel.write_bytes(b"raw")

            with mock.patch("testvm.cli.run_vm", return_value=0) as run_mock:
                result = runner.invoke(app, ["run", str(kernel), "--arch", "arm"])

            self.assertEqual(result.exit_code, 0)
            run_mock.assert_called_once()
