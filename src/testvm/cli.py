from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import (
    Architecture,
    DEFAULT_BUSYBOX_REF,
    build_default_initrd,
    pack_initrd,
    run_vm,
    unpack_initrd,
)
from ._errors import TestvmError

app = typer.Typer(no_args_is_help=True)
initrd_app = typer.Typer(no_args_is_help=True)
app.add_typer(initrd_app, name="initrd")


def _exit_for_error(exc: TestvmError) -> None:
    typer.secho(str(exc), err=True, fg=typer.colors.RED)
    raise typer.Exit(code=2)


@initrd_app.command("pack")
def pack_command(
    rootfs: Annotated[Path, typer.Argument(help="Root filesystem directory.")],
    output: Annotated[Path, typer.Argument(help="Output initrd path.")],
) -> None:
    try:
        path = pack_initrd(rootfs, output)
    except TestvmError as exc:
        _exit_for_error(exc)
    typer.echo(path)


@initrd_app.command("unpack")
def unpack_command(
    initrd: Annotated[Path, typer.Argument(help="Input initrd path.")],
    output_dir: Annotated[Path, typer.Argument(help="Destination directory.")],
) -> None:
    try:
        path = unpack_initrd(initrd, output_dir)
    except TestvmError as exc:
        _exit_for_error(exc)
    typer.echo(path)


@initrd_app.command("build-default")
def build_default_command(
    arch: Annotated[Architecture, typer.Option(help="Target architecture.")],
    output: Annotated[Path | None, typer.Option(help="Output initrd path.")] = None,
    workdir: Annotated[Path | None, typer.Option(help="BusyBox work directory.")] = None,
    force_rebuild: Annotated[
        bool, typer.Option("--force-rebuild", help="Rebuild cached BusyBox artifacts.")
    ] = False,
    busybox_ref: Annotated[
        str, typer.Option(help="BusyBox branch, tag, or commit-ish to clone.")
    ] = DEFAULT_BUSYBOX_REF,
) -> None:
    try:
        path = build_default_initrd(
            arch=arch,
            output_path=output,
            workdir=workdir,
            force_rebuild=force_rebuild,
            busybox_ref=busybox_ref,
        )
    except TestvmError as exc:
        _exit_for_error(exc)
    typer.echo(path)


@app.command("run")
def run_command(
    vmlinux: Annotated[
        Path,
        typer.Argument(help="Kernel image to boot. Non-ELF images require --arch."),
    ],
    arch: Annotated[Architecture | None, typer.Option(help="Override detected architecture.")] = None,
    initrd: Annotated[Path | None, typer.Option(help="Initrd path to boot with.")] = None,
    workdir: Annotated[Path | None, typer.Option(help="BusyBox work directory.")] = None,
    gdb_port: Annotated[int | None, typer.Option(help="Enable QEMU gdb stub on this TCP port.")] = None,
    memory: Annotated[str, typer.Option(help="Guest memory size.")] = "512M",
    smp: Annotated[int, typer.Option(help="Guest vCPU count.")] = 1,
    append: Annotated[
        list[str] | None,
        typer.Option(help="Additional kernel command line arguments. Repeatable."),
    ] = None,
    qemu_arg: Annotated[
        list[str] | None,
        typer.Option(help="Additional raw QEMU arguments. Repeatable."),
    ] = None,
    force_rebuild_initrd: Annotated[
        bool, typer.Option("--force-rebuild-initrd", help="Rebuild the auto-generated initrd.")
    ] = False,
) -> None:
    try:
        exit_code = run_vm(
            kernel=vmlinux,
            arch=arch,
            initrd=initrd,
            gdb_port=gdb_port,
            memory=memory,
            smp=smp,
            append=append or (),
            qemu_arg=qemu_arg or (),
            workdir=workdir,
            force_rebuild_initrd=force_rebuild_initrd,
        )
    except TestvmError as exc:
        _exit_for_error(exc)
    raise typer.Exit(code=exit_code)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
