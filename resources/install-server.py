#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

PLATFORM = "{{ PLATFORM }}"
COMMIT = "{{ COMMIT }}"
VERSION = "{{ VERSION }}"

ext = ".tar.gz" if PLATFORM.startswith(("linux", "alpine")) else ".zip"

server_platform = PLATFORM.replace("alpine", "linux")

DATA_DIR = Path(__file__).parent
DATA_DIST_DIR = DATA_DIR / "dist" / COMMIT
DATA_CLI_TAR = DATA_DIST_DIR / f"vscode-cli-{PLATFORM}-cli{ext}"
DATA_SERVER_TAR = DATA_DIST_DIR / f"code-server-{server_platform}-{VERSION}{ext}"


def universal_extract(archive: Path, dest: Path) -> None:
    # extract the archive to the destination
    # if it is a tar file, apply the tar filter to copy permissions
    kwargs = {}
    if archive.suffix != ".zip":
        kwargs["filter"] = "tar"
    shutil.unpack_archive(archive, dest, **kwargs)


def get_default_install_mode() -> str:
    # auto detect the install mode
    # WSL and container envs use legacy mode
    if (
        # wsl
        "WSL_DISTRO_NAME" in os.environ
        # podman
        or "container" in os.environ
        # docker
        or os.path.exists("/.docker-env")
    ):
        return "bin"

    # vscode default config uses cli mode for SSH connections
    # if the user manually set remote.SSH.useExecServer to false, they should
    # use the legacy mode by providing the --legacy flag
    return "cli"


def install_server(code_home: Path, install_mode: str | None = None) -> None:
    # install the server to the code home directory
    # if the install mode is not provided, auto detect it
    if install_mode is None:
        install_mode = get_default_install_mode()

    code_home.mkdir(parents=True, exist_ok=True)

    # standard paths
    server_path = code_home / f"cli/servers/Stable-{COMMIT}/server"
    server_legacy_path = code_home / "bin" / COMMIT
    cli_path = code_home / f"code-{COMMIT}"

    if install_mode == "cli" and not cli_path.is_file():
        print(f"Installing CLI to {cli_path}")
        with tempfile.TemporaryDirectory() as tempdir:
            tempdir = Path(tempdir)
            universal_extract(DATA_CLI_TAR, tempdir)
            shutil.move(tempdir / "code", cli_path)

    # the server dir changes based on the install mode
    server_dir = server_path if install_mode == "cli" else server_legacy_path

    if not server_dir.is_dir():
        print(f"Installing server to {server_dir}")
        with tempfile.TemporaryDirectory() as tempdir:
            tempdir = Path(tempdir)
            universal_extract(DATA_SERVER_TAR, tempdir)

            shutil.move(tempdir / f"vscode-server-{server_platform}", server_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--code-home",
        type=Path,
        default=Path.home() / ".vscode-server",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--legacy",
        action="store_const",
        dest="install_mode",
        const="bin",
        help="""
        Force legacy install mode. Select this if the config 'remote.SSH.useExecServer'
        is manually set to false.
        """,
    )
    group.add_argument("--cli", action="store_const", dest="install_mode", const="cli")
    group.set_defaults(install_mode=None)
    args = parser.parse_args()

    install_server(args.code_home.resolve(), args.install_mode)


if __name__ == "__main__":
    main()
