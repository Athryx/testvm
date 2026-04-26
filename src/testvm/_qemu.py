from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address, ip_interface
from pathlib import Path
from typing import Iterable

from ._arch import Architecture, detect_kernel_arch, normalize_arch
from ._busybox import build_default_initrd
from ._errors import CommandExecutionError, TestvmError
from ._ext4 import pack_ext4_image, unpack_ext4_image
from ._initrd import _build_composed_initrd

_SHARE_MOUNT_POINT = "/mnt/testvm-share"


class ShareMode(StrEnum):
    INITRD = "initrd"
    EXT4 = "ext4"


class NetworkMode(StrEnum):
    USER = "user"
    TAP = "tap"
    BRIDGE = "bridge"
    NONE = "none"


@dataclass(frozen=True)
class _HostForward:
    host_port: int
    guest_port: int


def normalize_share_mode(share_mode: str | ShareMode) -> ShareMode:
    if isinstance(share_mode, ShareMode):
        return share_mode

    normalized = share_mode.lower()
    try:
        return ShareMode(normalized)
    except ValueError as exc:
        raise TestvmError(f"Unsupported share mode: {share_mode}") from exc


def normalize_network_mode(network: str | NetworkMode) -> NetworkMode:
    if isinstance(network, NetworkMode):
        return network

    normalized = network.lower()
    try:
        return NetworkMode(normalized)
    except ValueError as exc:
        raise TestvmError(f"Unsupported network mode: {network}") from exc


def _validate_autorun_path(path: str) -> str:
    if not path.startswith("/"):
        raise TestvmError(f"Autorun path must be absolute inside the guest: {path}")
    if any(character.isspace() for character in path):
        raise TestvmError(f"Autorun path may not contain whitespace: {path}")
    return path


def _validate_port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise TestvmError(f"Invalid TCP port: {value}") from exc
    if port < 1 or port > 65535:
        raise TestvmError(f"TCP port out of range: {value}")
    return port


def _parse_host_forward(value: str) -> _HostForward:
    parts = value.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise TestvmError(f"Host forward must be HOST_PORT:GUEST_PORT: {value}")
    return _HostForward(
        host_port=_validate_port(parts[0]),
        guest_port=_validate_port(parts[1]),
    )


def _validate_ip(value: str, *, name: str) -> str:
    try:
        return str(ip_address(value))
    except ValueError as exc:
        raise TestvmError(f"Invalid {name}: {value}") from exc


def _validate_cidr(value: str) -> tuple[str, str]:
    try:
        interface = ip_interface(value)
    except ValueError as exc:
        raise TestvmError(f"Invalid network IP CIDR: {value}") from exc
    return str(interface.ip), str(interface.network.netmask)


def _network_device_for_arch(arch: Architecture) -> str:
    if arch is Architecture.X86_64:
        return "virtio-net-pci"
    return "virtio-net-device"


def _resolve_network_configuration(
    *,
    arch: Architecture,
    network: str | NetworkMode,
    network_tap: str | None,
    network_bridge: str | None,
    hostfwd: Iterable[str],
    network_ip: str | None,
    network_gateway: str | None,
    network_dns: Iterable[str],
    network_host_ip: str | None,
) -> tuple[list[str], list[str]]:
    normalized_network = normalize_network_mode(network)
    host_forwards = [_parse_host_forward(item) for item in hostfwd]
    dns_servers = [_validate_ip(item, name="DNS server") for item in network_dns]
    guest_ip = None if network_ip is None else _validate_cidr(network_ip)
    gateway = (
        None if network_gateway is None else _validate_ip(network_gateway, name="gateway")
    )
    host_ip = (
        None if network_host_ip is None else _validate_ip(network_host_ip, name="host IP")
    )

    if normalized_network is not NetworkMode.TAP and network_tap is not None:
        raise TestvmError("--network-tap requires --network tap")
    if normalized_network is not NetworkMode.BRIDGE and network_bridge is not None:
        raise TestvmError("--network-bridge requires --network bridge")
    if normalized_network is not NetworkMode.USER and host_forwards:
        raise TestvmError("--hostfwd requires --network user")
    if normalized_network is NetworkMode.NONE:
        if guest_ip is not None or gateway is not None or dns_servers or host_ip is not None:
            raise TestvmError("Network configuration options require networking")
        return ["-nic", "none"], []
    if normalized_network is NetworkMode.TAP and not network_tap:
        raise TestvmError("--network tap requires --network-tap")
    if normalized_network is NetworkMode.BRIDGE and not network_bridge:
        raise TestvmError("--network bridge requires --network-bridge")
    if gateway is not None and guest_ip is None:
        raise TestvmError("--network-gateway requires --network-ip")
    if dns_servers and guest_ip is None:
        raise TestvmError("--network-dns requires --network-ip")

    netdev_options: list[str]
    if normalized_network is NetworkMode.USER:
        netdev_options = ["user", "id=testvm-net0"]
        for forward in host_forwards:
            netdev_options.append(
                f"hostfwd=tcp::{forward.host_port}-:{forward.guest_port}"
            )
        host_ip = "10.0.2.2" if host_ip is None else host_ip
    elif normalized_network is NetworkMode.TAP:
        netdev_options = [
            "tap",
            "id=testvm-net0",
            f"ifname={network_tap}",
            "script=no",
            "downscript=no",
        ]
    else:
        netdev_options = ["bridge", "id=testvm-net0", f"br={network_bridge}"]

    qemu_args = [
        "-netdev",
        ",".join(netdev_options),
        "-device",
        f"{_network_device_for_arch(arch)},netdev=testvm-net0",
    ]
    cmdline = ["testvm_net=1"]
    if normalized_network is NetworkMode.USER:
        cmdline.append("testvm_net_user=1")
    if guest_ip is None:
        cmdline.append("testvm_net_config=dhcp")
    else:
        guest_address, guest_netmask = guest_ip
        cmdline.extend(
            [
                "testvm_net_config=static",
                f"testvm_net_ip={guest_address}",
                f"testvm_net_netmask={guest_netmask}",
            ]
        )
        if gateway is not None:
            cmdline.append(f"testvm_net_gateway={gateway}")
        if dns_servers:
            cmdline.append(f"testvm_net_dns={','.join(dns_servers)}")
    if host_ip is not None:
        cmdline.append(f"testvm_net_host={host_ip}")
    return qemu_args, cmdline


def _resolve_share_configuration(
    *,
    share_dir: Path | None,
    autorun_vm_path: str | None,
    autorun_path: Path | None,
) -> tuple[Path | None, str | None, Path | None]:
    resolved_share = None if share_dir is None else share_dir.expanduser().resolve()
    resolved_autorun = (
        None if autorun_vm_path is None else _validate_autorun_path(autorun_vm_path)
    )
    autorun_only_path: Path | None = None

    if autorun_path is not None:
        if resolved_autorun is not None:
            raise TestvmError("--autorun cannot be used together with --autorun-vm-path")

        host_path = autorun_path.expanduser().resolve()
        if not host_path.is_file():
            raise TestvmError(f"Host autorun path does not exist: {host_path}")

        if resolved_share is None:
            autorun_only_path = host_path
            resolved_autorun = _validate_autorun_path(
                f"{_SHARE_MOUNT_POINT}/{host_path.name}"
            )
        else:
            if not resolved_share.is_dir():
                raise TestvmError(f"Shared directory does not exist: {resolved_share}")

            try:
                relative = host_path.relative_to(resolved_share)
            except ValueError as exc:
                raise TestvmError(
                    f"Host autorun path must be inside the shared directory: {host_path}"
                ) from exc
            resolved_autorun = _validate_autorun_path(
                f"{_SHARE_MOUNT_POINT}/{relative.as_posix()}"
            )

    if resolved_share is not None and not resolved_share.is_dir():
        raise TestvmError(f"Shared directory does not exist: {resolved_share}")

    return resolved_share, resolved_autorun, autorun_only_path


def _replace_directory_contents(destination: Path, source: Path) -> None:
    for child in destination.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True, copy_function=shutil.copy2)
        elif child.is_symlink():
            target.symlink_to(child.readlink())
        else:
            shutil.copy2(child, target)


def _build_qemu_command(
    *,
    kernel: Path,
    arch: Architecture | str,
    initrd: Path | None,
    gdb_port: int | None,
    memory: str,
    smp: int,
    append: Iterable[str],
    nokaslr: bool,
    qemu_arg: Iterable[str],
    share_image: Path | None = None,
    autorun: str | None = None,
    network: str | NetworkMode = NetworkMode.USER,
    network_tap: str | None = None,
    network_bridge: str | None = None,
    hostfwd: Iterable[str] = (),
    network_ip: str | None = None,
    network_gateway: str | None = None,
    network_dns: Iterable[str] = (),
    network_host_ip: str | None = None,
) -> list[str]:
    arch = normalize_arch(arch)
    if arch is Architecture.X86_64:
        command = [
            "qemu-system-x86_64",
            "-m",
            memory,
            "-smp",
            str(smp),
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(kernel),
        ]
        cmdline = ["console=ttyS0", "rdinit=/init"]
    elif arch is Architecture.ARM:
        command = [
            "qemu-system-arm",
            "-machine",
            "virt",
            "-cpu",
            "cortex-a15",
            "-m",
            memory,
            "-smp",
            str(smp),
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(kernel),
        ]
        cmdline = ["console=ttyAMA0", "rdinit=/init"]
    elif arch is Architecture.AARCH64:
        command = [
            "qemu-system-aarch64",
            "-machine",
            "virt",
            "-cpu",
            "max",
            "-m",
            memory,
            "-smp",
            str(smp),
            "-nographic",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-kernel",
            str(kernel),
        ]
        cmdline = ["console=ttyAMA0", "rdinit=/init"]
    else:
        raise TestvmError(f"Unsupported architecture: {arch}")

    if initrd is not None:
        command.extend(["-initrd", str(initrd)])
    if gdb_port is not None:
        command.extend(["-S", "-gdb", f"tcp::{gdb_port}"])
    network_args, network_cmdline = _resolve_network_configuration(
        arch=arch,
        network=network,
        network_tap=network_tap,
        network_bridge=network_bridge,
        hostfwd=hostfwd,
        network_ip=network_ip,
        network_gateway=network_gateway,
        network_dns=network_dns,
        network_host_ip=network_host_ip,
    )
    command.extend(network_args)
    cmdline.extend(network_cmdline)
    if share_image is not None:
        command.extend(["-drive", f"file={share_image},format=raw,if=virtio"])
        cmdline.append("testvm_share=1")
    if autorun is not None:
        cmdline.append(f"testvm_autorun={_validate_autorun_path(autorun)}")
    if nokaslr:
        cmdline.append("nokaslr")

    cmdline.extend(str(item) for item in append)
    command.extend(["-append", " ".join(cmdline)])
    command.extend(str(item) for item in qemu_arg)
    return command


def run_vm(
    *,
    kernel: str | Path,
    arch: Architecture | str | None = None,
    initrd: str | Path | None = None,
    gdb_port: int | None = None,
    memory: str = "512M",
    smp: int = 1,
    append: Iterable[str] = (),
    nokaslr: bool = False,
    qemu_arg: Iterable[str] = (),
    network: str | NetworkMode = NetworkMode.USER,
    network_tap: str | None = None,
    network_bridge: str | None = None,
    hostfwd: Iterable[str] = (),
    network_ip: str | None = None,
    network_gateway: str | None = None,
    network_dns: Iterable[str] = (),
    network_host_ip: str | None = None,
    module_initrd: str | Path | None = None,
    share_dir: str | Path | None = None,
    share_mode: str | ShareMode = ShareMode.INITRD,
    sync_share_back: bool = False,
    autorun_vm_path: str | None = None,
    autorun_path: str | Path | None = None,
    force_rebuild_initrd: bool = False,
) -> int:
    hostfwd_values = tuple(hostfwd)
    network_dns_values = tuple(network_dns)
    kernel_path = Path(kernel).expanduser().resolve()
    if not kernel_path.is_file():
        raise TestvmError(f"Kernel does not exist: {kernel_path}")
    (
        resolved_share_dir,
        resolved_autorun,
        autorun_only_path,
    ) = _resolve_share_configuration(
        share_dir=None if share_dir is None else Path(share_dir),
        autorun_vm_path=autorun_vm_path,
        autorun_path=None if autorun_path is None else Path(autorun_path),
    )
    if sync_share_back and resolved_share_dir is None:
        raise TestvmError("--sync-share-back requires --share-dir")
    normalized_share_mode = normalize_share_mode(share_mode)
    if sync_share_back and normalized_share_mode is not ShareMode.EXT4:
        raise TestvmError("--sync-share-back requires --share-mode ext4")

    normalized_arch = (
        detect_kernel_arch(kernel_path) if arch is None else normalize_arch(arch)
    )
    _resolve_network_configuration(
        arch=normalized_arch,
        network=network,
        network_tap=network_tap,
        network_bridge=network_bridge,
        hostfwd=hostfwd_values,
        network_ip=network_ip,
        network_gateway=network_gateway,
        network_dns=network_dns_values,
        network_host_ip=network_host_ip,
    )

    initrd_path: Path | None
    if initrd is not None:
        initrd_path = Path(initrd).expanduser().resolve()
        if not initrd_path.is_file():
            raise TestvmError(f"Initrd does not exist: {initrd_path}")
    else:
        initrd_path = build_default_initrd(
            arch=normalized_arch,
            force_rebuild=force_rebuild_initrd,
        )

    with tempfile.TemporaryDirectory(prefix="testvm-share-") as temp_dir:
        effective_share_dir = resolved_share_dir
        if autorun_only_path is not None:
            autorun_share_dir = Path(temp_dir) / "autorun-share"
            autorun_share_dir.mkdir()
            shutil.copy2(autorun_only_path, autorun_share_dir / autorun_only_path.name)
            effective_share_dir = autorun_share_dir

        if module_initrd is not None or (
            effective_share_dir is not None and normalized_share_mode is ShareMode.INITRD
        ):
            initrd_path = _build_composed_initrd(
                initrd_path,
                output_path=Path(temp_dir) / "merged-initrd.cpio.gz",
                module_overlay=module_initrd,
                shared_dir=(
                    effective_share_dir
                    if normalized_share_mode is ShareMode.INITRD
                    else None
                ),
            )

        share_image: Path | None = None
        if effective_share_dir is not None and normalized_share_mode is ShareMode.EXT4:
            share_image = pack_ext4_image(
                effective_share_dir, Path(temp_dir) / "share.img"
            )

        command = _build_qemu_command(
            kernel=kernel_path,
            arch=normalized_arch,
            initrd=initrd_path,
            gdb_port=gdb_port,
            memory=memory,
            smp=smp,
            append=append,
            nokaslr=nokaslr,
            qemu_arg=qemu_arg,
            network=network,
            network_tap=network_tap,
            network_bridge=network_bridge,
            hostfwd=hostfwd_values,
            network_ip=network_ip,
            network_gateway=network_gateway,
            network_dns=network_dns_values,
            network_host_ip=network_host_ip,
            share_image=share_image,
            autorun=resolved_autorun,
        )
        try:
            result = subprocess.run(command, check=False)
        except FileNotFoundError as exc:
            raise CommandExecutionError(f"Command not found: {command[0]}") from exc

        if (
            sync_share_back
            and resolved_share_dir is not None
            and share_image is not None
        ):
            sync_dir = Path(temp_dir) / "share-out"
            unpack_ext4_image(share_image, sync_dir)
            _replace_directory_contents(resolved_share_dir, sync_dir)

        if result.returncode < 0:
            raise CommandExecutionError(f"QEMU exited with signal {-result.returncode}")
        return result.returncode
