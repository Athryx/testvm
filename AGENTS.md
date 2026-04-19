# Repository Guidelines

## Project Structure & Module Organization
`testvm` is a `src/`-layout Python package. Core library code lives in `src/testvm/`: `cli.py` exposes the Typer CLI, `_initrd.py` and `_ext4.py` handle image packing, `_busybox.py` builds the default initrd, `_qemu.py` launches guests, and `_arch.py` centralizes architecture detection. Public exports are re-exported from `src/testvm/__init__.py`.

Tests live in `tests/` and are split by API vs. CLI coverage (`test_api.py`, `test_cli.py`). Use `samples/` only for local example artifacts; avoid committing large generated images or unpacked root filesystems unless they are intentional fixtures.

## Build, Test, and Development Commands
Use `uv` for local development:

- `uv sync` installs the package and dependencies into the project environment.
- `uv run python -m unittest discover -s tests` runs the full test suite.
- `uv run testvm --help` verifies the CLI entrypoint and available subcommands.
- `uv run testvm initrd build-default --arch x86_64` builds the default BusyBox initrd.
- `uv run testvm run ./vmlinux --gdb-port 1234` boots an ELF kernel under QEMU.
- `uv run testvm run ./vmlinux --nokaslr` disables KASLR for a boot.
- `uv run testvm run ./zImage` boots a raw kernel image when `file(1)` can identify its architecture.

Host tools matter here: Docker, `file`/`libmagic`, `cpio`, `debugfs`, `gzip`, `lz4`, `mkfs.ext4`, and the relevant `qemu-system-*` binaries must be installed.

## Coding Style & Naming Conventions
Target Python 3.13, use 4-space indentation, and keep type hints on public functions. Follow the existing style: `pathlib.Path` over raw strings for filesystem paths, `snake_case` for functions and variables, `UPPER_CASE` for constants, and small focused helpers in private `_*.py` modules.

Keep CLI commands thin: argument parsing and user-facing errors belong in `cli.py`; filesystem, Docker, and QEMU behavior belong in the library modules.

## Testing Guidelines
Tests use `unittest` with `unittest.mock`. Add or update tests in `tests/test_api.py` for library behavior and `tests/test_cli.py` for command wiring. Name new test methods `test_<behavior>` and prefer temporary directories over fixed paths. Cover both success paths and error handling when touching packing, unpacking, VM launch flows, or architecture detection.

## Commit & Pull Request Guidelines
Recent commits use short, lowercase subjects such as `added lz4 support for initrd decompression`. Keep commits narrowly scoped and describe the user-visible change in the subject line.

Pull requests should explain the workflow affected, list the commands you ran, and call out any required host dependencies or architecture-specific testing. Include terminal snippets only when CLI output changed materially.
