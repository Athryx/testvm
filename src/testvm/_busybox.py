from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ._arch import Architecture, normalize_arch
from ._errors import CommandExecutionError, TestvmError
from ._initrd import pack_initrd
from ._paths import get_data_dir

BUSYBOX_GIT_URL = "https://git.busybox.net/busybox"
DEFAULT_BUSYBOX_REF = "1_37_stable"
DOCKER_IMAGE_TAG = "testvm-busybox-builder:ubuntu24.04"
DOCKER_SOURCE_DIR = Path("/workspace/source")
DOCKER_BUILD_DIR = Path("/workspace/build")
DOCKER_ROOTFS_DIR = Path("/workspace/rootfs")
_BUSYBOX_MAKE_VARS = {
    Architecture.X86_64: [],
    Architecture.ARM: ["ARCH=arm", "CROSS_COMPILE=arm-linux-gnueabihf-"],
    Architecture.AARCH64: ["ARCH=arm64", "CROSS_COMPILE=aarch64-linux-gnu-"],
}
_REQUIRED_BUSYBOX_CONFIGS = [
    "CONFIG_IFCONFIG",
    "CONFIG_ROUTE",
    "CONFIG_UDHCPC",
    "CONFIG_PING",
    "CONFIG_NC",
]


def _enable_busybox_config(option: str) -> list[str]:
    return [
        f"if grep -q '^# {option} is not set$' {DOCKER_BUILD_DIR}/.config; then",
        f"    sed -i 's/^# {option} is not set$/{option}=y/' {DOCKER_BUILD_DIR}/.config",
        f"elif ! grep -q '^{option}=y$' {DOCKER_BUILD_DIR}/.config; then",
        f"    printf '\\n{option}=y\\n' >> {DOCKER_BUILD_DIR}/.config",
        "fi",
    ]


def _run_checked(command: list[str], *, cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise CommandExecutionError(f"Command not found: {command[0]}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        message = stderr or stdout or "command failed"
        raise CommandExecutionError(f"{' '.join(command)}: {message}")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _docker_bind_mount(source: Path, target: Path) -> str:
    return f"type=bind,src={source},dst={target}"


def _docker_user_args() -> list[str]:
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return []
    return ["--user", f"{getuid()}:{getgid()}"]


def _run_docker_checked(command: list[str]) -> None:
    try:
        _run_checked(command)
    except CommandExecutionError as exc:
        message = str(exc)
        if "Command not found: docker" in message:
            raise TestvmError(
                "Docker CLI not found; install Docker and ensure `docker` is on PATH"
            ) from exc
        if "permission denied while trying to connect to the docker API" in message:
            raise TestvmError(
                "Docker daemon access failed; ensure your user can access the Docker socket"
            ) from exc
        raise


def _ensure_docker_builder_image() -> None:
    repo_root = _repo_root()
    dockerfile = repo_root / "Dockerfile"
    _run_docker_checked(
        [
            "docker",
            "build",
            "--tag",
            DOCKER_IMAGE_TAG,
            "--file",
            str(dockerfile),
            str(repo_root),
        ]
    )


def _build_busybox_in_docker(
    *,
    arch: Architecture,
    source_dir: Path,
    build_dir: Path,
    rootfs_dir: Path,
) -> None:
    make_vars = " ".join(_BUSYBOX_MAKE_VARS[arch])
    make_prefix = f"make -C {DOCKER_SOURCE_DIR} O={DOCKER_BUILD_DIR}"
    if make_vars:
        make_prefix = f"{make_prefix} {make_vars}"
    extra_config_steps: list[str] = []
    if arch is not Architecture.X86_64:
        extra_config_steps.extend(
            [
                f"if grep -q '^CONFIG_SHA1_HWACCEL=y$' {DOCKER_BUILD_DIR}/.config; then",
                f"    sed -i 's/^CONFIG_SHA1_HWACCEL=y$/# CONFIG_SHA1_HWACCEL is not set/' {DOCKER_BUILD_DIR}/.config",
                "fi",
                f"if grep -q '^CONFIG_SHA256_HWACCEL=y$' {DOCKER_BUILD_DIR}/.config; then",
                f"    sed -i 's/^CONFIG_SHA256_HWACCEL=y$/# CONFIG_SHA256_HWACCEL is not set/' {DOCKER_BUILD_DIR}/.config",
                "fi",
            ]
        )
    build_steps = "\n".join(
        [
            "set -eu",
            f"{make_prefix} defconfig",
            f"if grep -q '^# CONFIG_STATIC is not set$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^# CONFIG_STATIC is not set$/CONFIG_STATIC=y/' {DOCKER_BUILD_DIR}/.config",
            "elif ! grep -q '^CONFIG_STATIC=y$' "
            f"{DOCKER_BUILD_DIR}/.config; then",
            f"    printf '\\nCONFIG_STATIC=y\\n' >> {DOCKER_BUILD_DIR}/.config",
            "fi",
            f"if grep -q '^CONFIG_TC=y$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^CONFIG_TC=y$/# CONFIG_TC is not set/' {DOCKER_BUILD_DIR}/.config",
            "fi",
            f"if grep -q '^# CONFIG_MODPROBE is not set$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^# CONFIG_MODPROBE is not set$/CONFIG_MODPROBE=y/' {DOCKER_BUILD_DIR}/.config",
            f"elif ! grep -q '^CONFIG_MODPROBE=y$' {DOCKER_BUILD_DIR}/.config; then",
            f"    printf '\\nCONFIG_MODPROBE=y\\n' >> {DOCKER_BUILD_DIR}/.config",
            "fi",
            f"if grep -q '^# CONFIG_INSMOD is not set$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^# CONFIG_INSMOD is not set$/CONFIG_INSMOD=y/' {DOCKER_BUILD_DIR}/.config",
            f"elif ! grep -q '^CONFIG_INSMOD=y$' {DOCKER_BUILD_DIR}/.config; then",
            f"    printf '\\nCONFIG_INSMOD=y\\n' >> {DOCKER_BUILD_DIR}/.config",
            "fi",
            f"if grep -q '^CONFIG_MODPROBE_SMALL=y$' {DOCKER_BUILD_DIR}/.config; then",
            f"    sed -i 's/^CONFIG_MODPROBE_SMALL=y$/# CONFIG_MODPROBE_SMALL is not set/' {DOCKER_BUILD_DIR}/.config",
            "fi",
            *[
                line
                for option in _REQUIRED_BUSYBOX_CONFIGS
                for line in _enable_busybox_config(option)
            ],
            *extra_config_steps,
            f"{make_prefix} silentoldconfig",
            f"{make_prefix} -j{os.cpu_count() or 1}",
            f"{make_prefix} CONFIG_PREFIX={DOCKER_ROOTFS_DIR} install",
        ]
    )

    command = [
        "docker",
        "run",
        "--rm",
        *_docker_user_args(),
        "--mount",
        _docker_bind_mount(source_dir, DOCKER_SOURCE_DIR),
        "--mount",
        _docker_bind_mount(build_dir, DOCKER_BUILD_DIR),
        "--mount",
        _docker_bind_mount(rootfs_dir, DOCKER_ROOTFS_DIR),
        DOCKER_IMAGE_TAG,
        "sh",
        "-lc",
        build_steps,
    ]
    _run_docker_checked(command)


def _ensure_busybox_source(source_dir: Path, busybox_ref: str) -> None:
    if source_dir.exists():
        return

    source_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            busybox_ref,
            BUSYBOX_GIT_URL,
            str(source_dir),
        ]
    )


def _write_init_script(rootfs_dir: Path) -> None:
    init_path = rootfs_dir / "init"
    script = """#!/bin/sh
set -eu

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true

mkdir -p /dev /proc /sys /run /tmp /root
[ -c /dev/console ] || mknod -m 600 /dev/console c 5 1

export PATH=/bin:/sbin:/usr/bin:/usr/sbin

share_requested=0
autorun_path=""
network_requested=0
network_user=0
network_config="dhcp"
network_ip=""
network_netmask=""
network_gateway=""
network_dns=""
network_host=""

for arg in $(cat /proc/cmdline); do
    case "$arg" in
        testvm_share=1)
            share_requested=1
            ;;
        testvm_autorun=*)
            autorun_path=${arg#testvm_autorun=}
            ;;
        testvm_net=1)
            network_requested=1
            ;;
        testvm_net_user=1)
            network_user=1
            ;;
        testvm_net_config=*)
            network_config=${arg#testvm_net_config=}
            ;;
        testvm_net_ip=*)
            network_ip=${arg#testvm_net_ip=}
            ;;
        testvm_net_netmask=*)
            network_netmask=${arg#testvm_net_netmask=}
            ;;
        testvm_net_gateway=*)
            network_gateway=${arg#testvm_net_gateway=}
            ;;
        testvm_net_dns=*)
            network_dns=${arg#testvm_net_dns=}
            ;;
        testvm_net_host=*)
            network_host=${arg#testvm_net_host=}
            ;;
    esac
done

write_resolv_conf() {
    mkdir -p /etc
    : > /etc/resolv.conf
    old_ifs=$IFS
    IFS=,
    for dns_server in $1; do
        if [ -n "$dns_server" ]; then
            echo "nameserver $dns_server" >> /etc/resolv.conf
        fi
    done
    IFS=$old_ifs
}

write_hosts_file() {
    mkdir -p /etc
    {
        echo "127.0.0.1 localhost"
        if [ -n "$network_host" ]; then
            echo "$network_host testvm-host"
        fi
    } > /etc/hosts
}

wait_for_eth0() {
    for _attempt in 1 2 3 4 5 6 7 8 9 10; do
        if [ -e /sys/class/net/eth0 ]; then
            return 0
        fi
        sleep 1
    done

    echo "warning: networking requested but eth0 was not found"
    return 1
}

configure_static_network() {
    if [ -z "$network_ip" ] || [ -z "$network_netmask" ]; then
        echo "warning: static networking requires testvm_net_ip and testvm_net_netmask"
        return 1
    fi

    ifconfig eth0 "$network_ip" netmask "$network_netmask" up
    if [ -n "$network_gateway" ]; then
        route add default gw "$network_gateway" dev eth0 2>/dev/null || true
    fi
    if [ -n "$network_dns" ]; then
        write_resolv_conf "$network_dns"
    fi
    write_hosts_file
    return 0
}

configure_dhcp_network() {
    udhcpc -i eth0 -s /etc/udhcpc/default.script -q -t 5 -T 1
}

configure_testvm_network() {
    if [ "$network_requested" != "1" ]; then
        return 0
    fi

    ifconfig lo up 2>/dev/null || true
    wait_for_eth0 || return 1
    ifconfig eth0 up 2>/dev/null || true

    if [ "$network_config" = "static" ]; then
        configure_static_network || return 1
    else
        if configure_dhcp_network; then
            write_hosts_file
        elif [ "$network_user" = "1" ]; then
            echo "warning: DHCP failed; using QEMU user-mode fallback address"
            network_ip=10.0.2.15
            network_netmask=255.255.255.0
            network_gateway=10.0.2.2
            network_dns=10.0.2.3
            configure_static_network || return 1
        else
            echo "warning: DHCP failed and no static network was configured"
            return 1
        fi
    fi

    echo "testvm network configured"
    ifconfig eth0 2>/dev/null || true
    if [ -n "$network_host" ]; then
        echo "testvm host reachable as testvm-host ($network_host)"
    fi
}

mount_testvm_share() {
    mkdir -p /mnt/testvm-share
    for _attempt in 1 2 3 4 5 6 7 8 9 10; do
        for device in /dev/vda /dev/vdb /dev/sda /dev/sdb /dev/xvda /dev/xvdb; do
            if [ -b "$device" ] && mount -t ext4 "$device" /mnt/testvm-share 2>/dev/null; then
                echo "testvm shared ext4 mounted from $device"
                return 0
            fi
        done
        sleep 1
    done

    echo "warning: testvm shared ext4 was requested but could not be mounted"
    return 1
}

if [ "$share_requested" = "1" ]; then
    mount_testvm_share || true
fi

configure_testvm_network || true

echo "testvm busybox initrd ready"

if [ -n "$autorun_path" ]; then
    if [ ! -e "$autorun_path" ]; then
        echo "warning: autorun path not found: $autorun_path"
    else
        set +e
        "$autorun_path"
        autorun_status=$?
        set -e
        echo "testvm autorun exited with status $autorun_status"
    fi
fi

exec /bin/sh
"""
    init_path.write_text(script)
    init_path.chmod(0o755)

    udhcpc_script = rootfs_dir / "etc" / "udhcpc" / "default.script"
    udhcpc_script.parent.mkdir(parents=True, exist_ok=True)
    udhcpc_script.write_text(
        """#!/bin/sh
set -eu

[ -n "${interface:-}" ] || exit 0

case "${1:-}" in
    deconfig)
        ifconfig "$interface" 0.0.0.0 2>/dev/null || true
        ;;
    bound|renew)
        mkdir -p /etc
        if [ -n "${broadcast:-}" ]; then
            ifconfig "$interface" "$ip" netmask "$subnet" broadcast "$broadcast" up
        else
            ifconfig "$interface" "$ip" netmask "$subnet" up
        fi
        route del default dev "$interface" 2>/dev/null || true
        for router_addr in ${router:-}; do
            route add default gw "$router_addr" dev "$interface" 2>/dev/null || true
            break
        done
        : > /etc/resolv.conf
        for dns_addr in ${dns:-}; do
            echo "nameserver $dns_addr" >> /etc/resolv.conf
        done
        ;;
esac
"""
    )
    udhcpc_script.chmod(0o755)


def build_default_initrd(
    *,
    arch: Architecture | str,
    output_path: str | Path | None = None,
    workdir: str | Path | None = None,
    force_rebuild: bool = False,
    busybox_ref: str = DEFAULT_BUSYBOX_REF,
) -> Path:
    normalized_arch = normalize_arch(arch)

    data_dir = get_data_dir()
    cache_root = data_dir / "busybox" / normalized_arch / busybox_ref
    cache_root.mkdir(parents=True, exist_ok=True)
    cached_initrd = cache_root / "initrd.cpio.gz"
    if workdir is None:
        work_path = data_dir / "work"
    else:
        work_path = Path(workdir).expanduser().resolve()
    work_path.mkdir(parents=True, exist_ok=True)
    source_dir = work_path / "busybox-src" / busybox_ref
    build_dir = work_path / "busybox-build" / normalized_arch / busybox_ref
    rootfs_dir = work_path / "busybox-rootfs" / normalized_arch / busybox_ref

    requested_output = (
        cached_initrd
        if output_path is None
        else Path(output_path).expanduser().resolve()
    )

    if not force_rebuild and requested_output.exists():
        return requested_output
    if not force_rebuild and cached_initrd.exists():
        if requested_output != cached_initrd:
            requested_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached_initrd, requested_output)
        return requested_output

    if force_rebuild:
        shutil.rmtree(build_dir, ignore_errors=True)
        shutil.rmtree(rootfs_dir, ignore_errors=True)

    _ensure_busybox_source(source_dir, busybox_ref)
    build_dir.mkdir(parents=True, exist_ok=True)
    rootfs_dir.mkdir(parents=True, exist_ok=True)

    _ensure_docker_builder_image()
    _build_busybox_in_docker(
        arch=normalized_arch,
        source_dir=source_dir,
        build_dir=build_dir,
        rootfs_dir=rootfs_dir,
    )

    for relative in ("proc", "sys", "dev", "run", "tmp", "root"):
        (rootfs_dir / relative).mkdir(parents=True, exist_ok=True)
    _write_init_script(rootfs_dir)

    pack_initrd(rootfs_dir, cached_initrd, compress=True)
    if requested_output != cached_initrd:
        requested_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached_initrd, requested_output)
    return requested_output
