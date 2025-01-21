#!/usr/bin/env python
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "rich",
# ]
# ///
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import traceback
import typing as t
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if t.TYPE_CHECKING:
    from rich.progress import TaskID

try:
    import rich
except ImportError:
    rich = None

T = t.TypeVar("T")

root = Path(__file__).parent.resolve()
resources = root / "resources"
workdir = root / "vscode-extensions"


IS_WSL = bool(os.getenv("WSL_DISTRO_NAME"))
DISABLE_RICH = bool(os.getenv("DISABLE_RICH"))

if DISABLE_RICH:
    rich = None


TARGET_PLATFORMS = [
    "win32-x64",
    "win32-arm64",
    "darwin-x64",
    "darwin-arm64",
    "linux-x64",
    "linux-arm64",
    "linux-armhf",
    "alpine-x64",
    "alpine-arm64",
]

TarFormat: t.TypeAlias = t.Literal["gz", "bz2", "xz", ""]
TAR_MODES: dict[str | tuple[str, ...], TarFormat] = {
    (".tar.gz", ".tgz"): "gz",
    (".tar.xz", ".txz"): "xz",
    (".tar.bz2", ".tbz2", ".tbz"): "bz2",
    ".tar": "",
}
TAR_EXTENSIONS = tuple(ext for exts in TAR_MODES for ext in exts)


PRODUCT_JSON_PATH = "resources/app/product.json"

FLATPAK_APPS: dict[str, tuple[str, str]] = {  # (name, app-home, extensions-dir)
    "com.visualstudio.code": ("extra/vscode", "vscode/extensions"),
    "com.visualstudio.code-oss": ("main", "vscode/extensions"),
    "com.vscodium.codium": ("share/codium", "codium/extensions"),
    "com.vscodium.codium-insiders": (
        "share/codium-insiders",
        "codium-insiders/extensions",
    ),
}


FLATPAK_PATHS = [
    "/var/lib/flatpak",
    "$XDG_DATA_HOME/flatpak",
    "~/.local/share/flatpak",
]

CODE_HOME_PATHS = [
    # Windows user install
    "%LOCALAPPDATA%\\Programs\\Microsoft VS Code",
    # Windows system install
    "%PROGRAMFILES%\\Microsoft VS Code",
    # Linux system installs
    "/usr/share/code",
    "/usr/lib/code",
    "/usr/share/vscodium",
    "/opt/code",
    "/opt/vscode",
    "/opt/visual-studio-code",
    "/opt/vscodium-bin",
    # Linux Flatpak Installs
    *(
        f"{path}/app/{name}/current/active/files/{app_home}"
        for path in FLATPAK_PATHS
        for name, (app_home, _) in FLATPAK_APPS.items()
    ),
    # Ubuntu Snap installs
    "/snap/code/current/usr/share/code",
]


file_log: set[Path] = set()

RESET = "\033[0m"
BOLD = "\033[1m"
ITALIC = "\033[3m"
DARK_RED = "\033[31m"
BG_YELLOW = "\033[43m"
GRAY = "\033[90m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
WHITE = "\033[97m"


class SafeThreadPoolExecutor(ThreadPoolExecutor):
    def __exit__(self, *exc_info: t.Any) -> None:
        if exc_info[0]:
            if isinstance(exc_info[1], KeyboardInterrupt):
                log.critical("Caught KeyboardInterrupt, shutting down")
            else:
                log.critical("Caught exception, shutting down", exc_info=exc_info)
            self.shutdown(wait=False, cancel_futures=True)

        super().__exit__(*exc_info)


class Progress:
    progress = None

    def __init__(self) -> None:
        if rich:
            from rich.progress import (
                BarColumn,
                DownloadColumn,
                Progress,
                TaskProgressColumn,
                TextColumn,
                TimeRemainingColumn,
            )

            self.progress = Progress(
                TextColumn("[progress.description]{task.description:80}"),
                BarColumn(),
                DownloadColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
            )

    def __enter__(self) -> t.Self:
        if self.progress:
            self.progress.start()
        return self

    def __exit__(self, *_: t.Any) -> None:
        if self.progress:
            self.progress.stop()

    @classmethod
    def track(cls, sequence: Iterable[T], **kwargs: t.Any) -> Iterable[T]:
        global rich
        if rich:
            from rich.progress import track

            return track(sequence, **kwargs)

        return sequence

    @contextlib.contextmanager
    def task(self, description: str) -> t.Generator[TaskID | None]:
        if self.progress:
            task = self.progress.add_task(description, total=None)
            try:
                yield task
            finally:
                self.progress.remove_task(task)
        else:
            yield None

    def urllib_callback(
        self, task: TaskID | None = None
    ) -> t.Callable[[int, int, int], None] | None:
        if self.progress and task is not None:
            return lambda blocknum, bs, size, progress=self.progress: progress.update(
                task, completed=blocknum * bs, total=size
            )
        return None


class ColorFormatter(logging.Formatter):
    colors: t.ClassVar = {
        "debug": GRAY,
        "info": BLUE,
        "warning": YELLOW,
        "error": RED,
        "critical": BOLD + ITALIC + DARK_RED + BG_YELLOW,
    }

    if sys.version_info >= (3, 13):

        def formatException(self, exc: t.Any) -> str:  # noqa: N802
            sio = io.StringIO()
            traceback.print_exception(*exc, file=sio, colorize=sys.stderr.isatty())  # type: ignore
            s = sio.getvalue()
            sio.close()
            return s.rstrip("\n")

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname.lower()
        prefix = f"{level.capitalize()}:"
        if record.levelno >= logging.CRITICAL:
            prefix = prefix.upper()
        text = super().format(record)

        prefix = prefix.ljust(10)
        if sys.stderr.isatty():
            color = self.colors.get(level, WHITE)
            prefix = prefix.replace(":", f":{RESET}")
            prefix = f"{color}{prefix}"
        return f"{prefix}{text}"


def init_logger() -> logging.Logger:
    log = logging.getLogger()
    log.setLevel(logging.INFO)

    if rich:
        from rich.logging import RichHandler

        handler = RichHandler(show_time=False, show_path=False)
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(ColorFormatter())

    log.addHandler(handler)
    return log


log = init_logger()


class ExtensionGallery(t.TypedDict):
    serviceUrl: str


class ProductJson(t.TypedDict):
    applicationName: str
    win32SetupExeBasename: t.NotRequired[str]
    win32DirName: str
    darwinExecutable: str
    version: str
    commit: str
    quality: str
    dataFolderName: str
    serverDataFolderName: str
    updateUrl: t.NotRequired[str]
    extensionsGallery: t.NotRequired[ExtensionGallery]


class ExtensionId(t.TypedDict):
    id: str
    uuid: t.NotRequired[str]


class ExtensionMeta(t.TypedDict, total=False):
    isApplicationScoped: bool
    isMachineScoped: bool
    isBuiltin: bool
    installedTimestamp: t.Required[int]
    targetPlatform: str
    pinned: bool
    source: str


class URI(t.TypedDict("BaseURI", {"$mid": int})):
    scheme: str
    path: str


class ExtensionData(t.TypedDict):
    identifier: ExtensionId
    version: str
    location: URI
    relativeLocation: str
    metadata: ExtensionMeta


class RemoteExtensionFile(t.TypedDict):
    assetType: str
    source: str


class RemoteExtensionVersion(t.TypedDict):
    version: str
    files: list[RemoteExtensionFile]
    targetPlatform: t.NotRequired[str]


class RemoteExtension(t.TypedDict):
    versions: list[RemoteExtensionVersion]


class AppError(Exception):
    pass


def expand_var_paths(paths: list[str]) -> Generator[Path, None, None]:
    for path in paths:
        if IS_WSL and "%" in path:
            path = win_path_to_wsl(resolve_win_interp(path))

        path = os.path.expandvars(path)
        path = os.path.expanduser(path)

        if "%" in path or "$" in path:
            # Ignore incomplete environment variables
            continue

        if (os.name != "nt" and ":\\" in path) or (
            os.name == "nt" and path.startswith("/")
        ):
            # Ignore non-windows path on windows
            continue

        yield Path(path)


def code_home_paths() -> Generator[Path, None, None]:
    """Expand environment variables and user home in a path.

    This function is aware of WSL and will expand windows environment variables not
    normally available in WSL.
    """
    return expand_var_paths(CODE_HOME_PATHS)


def get_vscode_home() -> Path:
    for path in code_home_paths():
        if (path / PRODUCT_JSON_PATH).exists():
            log.debug("Detected vscode home at %s", path)
            return path

    msg = (
        "Visual Studio Code not found. If it's installed, please specify the path with"
        " --code-home"
    )
    raise AppError(msg)


def resolve_win_interp(args: str) -> str:
    """Resolve windows environment variables through cmd.exe"""
    root_path = win_path_to_wsl("C:\\")
    # use the full path to cmd in case the user disabled windows interop or modified the
    # PATH
    cmd_path = win_path_to_wsl("C:\\Windows\\System32\\cmd.exe")
    return subprocess.check_output(
        [cmd_path, "/c", f"echo {args}"],
        encoding="utf-8",
        # cmd doesn't like being started with a WSL CWD
        cwd=root_path,
    ).strip()


def get_win_home() -> Path:
    return win_path_to_wsl(resolve_win_interp("%USERPROFILE%"))


def win_path_to_wsl(path: str) -> Path:
    return Path(
        subprocess.check_output(
            ["wslpath", "-u", path],
            encoding="utf-8",
        ).strip()
    )


def wsl_path_to_win(path: str | Path) -> str:
    return subprocess.check_output(
        ["wslpath", "-w", path],
        encoding="utf-8",
    ).strip()


def is_wsl_windows_mount(path: Path) -> bool:
    win_path = wsl_path_to_win(path)

    # windows mounts will include the drive letter. wsl paths will start with \\
    return not win_path.startswith(r"\\")


def flatpak_paths() -> Generator[Path, None, None]:
    return expand_var_paths(FLATPAK_PATHS)


def get_home(path: Path) -> Path:
    if IS_WSL and is_wsl_windows_mount(path):
        return get_win_home()

    for flatpak in flatpak_paths():
        if path.is_relative_to(flatpak):
            path = path.relative_to(flatpak)
            return Path.home() / f".var/app/{path.parts[0]}/data"

    return Path.home()


class Product:
    def __init__(self, code_home: Path, data: ProductJson) -> None:
        self.code_home = code_home
        self.data = data

        log.debug("Detected vscode version: %s", data["version"])

    @classmethod
    def load(cls, code_home: Path | None) -> t.Self:
        if code_home is None:
            code_home = get_vscode_home()
        product_json = code_home / PRODUCT_JSON_PATH
        log.debug("Loading product json from %s", product_json)
        try:
            with product_json.open() as f:
                return cls(code_home, json.load(f))
        except FileNotFoundError:
            msg = f"Product json not found at {product_json}"
            raise AppError(msg) from None

    def marketplace(
        self,
        marketplace_url: str | None,
    ) -> Marketplace | None:
        if marketplace_url is None:
            marketplace_url = self.data.get("extensionsGallery", {}).get("serviceUrl")

        if marketplace_url is not None:
            log.debug("Using marketplace url: %s", marketplace_url)
            return Marketplace(marketplace_url)

        msg = (
            "Unable to load marketplace service url from product.json"
            " Supply it via --marketplace-url"
        )
        raise AppError(msg)

    def distributions(
        self,
        update_url: str | None,
    ) -> Distributions | None:
        if update_url is None:
            update_url = self.data.get("updateUrl")

        if update_url is not None:
            log.debug("Using update url: %s", update_url)
            return Distributions(self, update_url)

        msg = "Unable to load update url from product.json. Supply it via --update-url"
        raise AppError(msg)

    def get_platform_server_name(self, platform: str) -> str:
        name = self.data["applicationName"]
        version = self.data["version"]
        ext = "tar.gz" if is_linux(platform) else "zip"
        if platform == "darwin-x64":
            platform = "darwin"
        return f"{name}-server-{platform}-{version}.{ext}"

    def get_platform_client_name(self, platform: str) -> str:
        version = self.data["version"]
        if platform == "ALL":
            return "(unknown)"
        if platform in ["win32-x64", "win32-arm64"]:
            default_user_setup = f"{self.data['win32DirName']}UserSetup"
            basename = self.data.get("win32SetupExeBasename", default_user_setup)
            return f"{basename}-user-{platform}-{version}.exe"

        if platform in ["darwin-x64", "darwin-arm64"]:
            basename = self.data["darwinExecutable"]
            platform = platform.removesuffix("-x64")
            return f"{basename}-{platform}-{version}.zip"

        basename = self.data["applicationName"]
        return f"{basename}-stable-{platform}-{version}.tar.gz"

    def get_data_folder(self) -> Path:
        home = get_home(self.code_home)
        for flatpak in flatpak_paths():
            if self.code_home.is_relative_to(flatpak):
                flatpak_name = self.code_home.relative_to(flatpak).parts[1]
                return home / FLATPAK_APPS[flatpak_name][1]

        return home / self.data["dataFolderName"]

    def load_extensions(self, extensions_dir: Path | None) -> Extensions:
        if extensions_dir is None:
            extensions_dir = self.get_data_folder() / "extensions"

        log.debug("Loading extensions from %s", extensions_dir)

        extensions_json = extensions_dir / "extensions.json"

        if not extensions_json.exists():
            return Extensions([])

        data: list[ExtensionData] = json.loads(extensions_json.read_text())
        data.sort(
            key=lambda ext: (
                ext["identifier"]["id"],
                ext["metadata"]["installedTimestamp"],
            )
        )

        # extensions can contain duplicates, so only include the most recently installed
        # version.
        extensions = list({ext["identifier"]["id"]: ext for ext in data}.values())

        return Extensions(extensions)


REMOTING_EXTENSION_IDS = [
    # official remoting plugins
    "ms-vscode-remote.vscode-remote-extensionpack",
    "ms-vscode-remote.remote-wsl",
    "ms-vscode-remote.remote-ssh",
    "ms-vscode-remote.remote-containers",
    # third party open source remoting plugins
    "jeanp413.open-remote-ssh",
    "jeanp413.open-remote-wsl",
]


class Extensions:
    def __init__(self, extensions: list[ExtensionData]) -> None:
        self.extensions = extensions

    def has_remoting_extension(self) -> bool:
        """Checks if any of the given plugins have a remoting extension"""

        return any(
            plugin["identifier"]["id"] in REMOTING_EXTENSION_IDS
            for plugin in self.extensions
        )


class Distributions:
    def __init__(self, product: Product, update_url: str) -> None:
        self.product = product
        self.update_url = update_url

        self.dist_dir = workdir / "dist" / self.product.data["commit"]
        self.dist_dir.mkdir(exist_ok=True, parents=True)

    def download_dist(
        self, dist: str, dest: Path, *, progress: Progress, executor: ThreadPoolExecutor
    ) -> None:
        file_log.add(dest.resolve())
        if dest.exists():
            return
        commit = self.product.data["commit"]
        url = f"{self.update_url}/commit:{commit}/{dist}/stable"
        executor.submit(download_file, url, dest, progress=progress)

    def download_client(
        self, target_platform: str, executor: ThreadPoolExecutor, progress: Progress
    ) -> None:
        for file in self.dist_dir.iterdir():
            if file.is_dir() and file.name != self.product.data["commit"]:
                log.info("Removing old dist version %s", file)
                shutil.rmtree(file)

        if target_platform.startswith("alpine"):
            target_platform = target_platform.replace("alpine", "linux")

        for platform in self.get_dist_platforms(target_platform):
            if platform.startswith("alpine"):
                continue

            self.download_dist(
                get_platform_client_download(platform),
                self.dist_dir / self.product.get_platform_client_name(platform),
                progress=progress,
                executor=executor,
            )

    def download_server(
        self, target_platform: str, executor: ThreadPoolExecutor, progress: Progress
    ) -> None:
        if target_platform.startswith("alpine"):
            target_platform = target_platform.replace("alpine", "linux")

        for platform in self.get_dist_platforms(target_platform):
            if platform.startswith("alpine"):
                continue
            name = self.product.get_platform_server_name(platform)
            self.download_dist(
                get_platform_server_download(platform),
                self.dist_dir / name,
                progress=progress,
                executor=executor,
            )

    def download_cli(
        self, target_platform: str, executor: ThreadPoolExecutor, progress: Progress
    ) -> None:
        for platform in self.get_dist_platforms(target_platform):
            ext = "zip" if platform.startswith("win32") else "tar.gz"
            self.download_dist(
                f"cli-{platform}",
                self.dist_dir / f"vscode-cli-{platform}-cli.{ext}",
                progress=progress,
                executor=executor,
            )

    @staticmethod
    def get_dist_platforms(target_platform: str) -> set[str]:
        if target_platform == "ALL":
            return set(TARGET_PLATFORMS)

        return {target_platform}


class Marketplace:
    def __init__(self, service_url: str) -> None:
        self.service_url = service_url

        self.extensions_dir = workdir / "extensions"
        self.extensions_dir.mkdir(exist_ok=True, parents=True)

    def _fetch_extension_data(self, ext_name: str) -> RemoteExtension | None:
        criteria = [
            # filter by extension name
            {"filterType": 7, "value": ext_name},
            # filter by target
            {"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
        ]
        ext_query_param = {
            "filters": [{"criteria": criteria}],
            # include versions, files
            "flags": 3,
        }

        with urllib.request.urlopen(
            urllib.request.Request(
                f"{self.service_url}/extensionquery",
                method="POST",
                data=json.dumps(ext_query_param).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json;api-version=3.0-preview.1",
                },
            )
        ) as response:
            data = json.load(response)

        if not data["results"][0]["extensions"]:
            return None
        return data["results"][0]["extensions"][0]

    def get_download_extension_urls(
        self, extension: ExtensionData
    ) -> t.Iterable[tuple[str, str]]:
        ext_name = extension["identifier"]["id"]
        data = self._fetch_extension_data(ext_name)
        if data is None:
            log.warning("Unable to find %s in marketplace. Skipping.", ext_name)
            return {}

        sources = {}
        for version in data["versions"]:
            if version["version"] == extension["version"]:
                for file in version["files"]:
                    if (
                        file["assetType"]
                        == "Microsoft.VisualStudio.Services.VSIXPackage"
                    ):
                        platform = version.get("targetPlatform", "universal")
                        sources[platform] = file["source"]
                        break

        if len(sources) > 1 and "universal" in sources:
            sources.pop("universal")

        return sources.items()

    def is_extension_cached(
        self, extension: ExtensionData, platforms: set[str]
    ) -> bool:
        name = extension["identifier"]["id"]
        vers = extension["version"]

        base = self.extensions_dir / name / vers

        file = base / f"{name}-{vers}.vsix"
        if file.exists():
            file_log.add(file.resolve())
            return True

        for platform in platforms:
            file = base / f"{name}-{vers}@{platform}.vsix"
            if not file.exists():
                return False
            file_log.add(file.resolve())

        return True

    def download_extensions(
        self,
        extensions: Extensions,
        platforms: set[str],
        executor: ThreadPoolExecutor,
        progress: Progress,
    ) -> None:
        for ext in extensions.extensions:
            if self.is_extension_cached(ext, platforms):
                continue

            for platform, url in self.get_download_extension_urls(ext):
                if platform != "universal" and platform not in platforms:
                    continue

                suffix = ""
                if platform != "universal":
                    suffix = f"@{platform}"

                base = self.extensions_dir / ext["identifier"]["id"] / ext["version"]
                file = f"{ext['identifier']['id']}-{ext['version']}{suffix}.vsix"
                fut = executor.submit(
                    download_file, url, base / file, progress=progress
                )
                fut.add_done_callback(
                    lambda _, ext=ext: self.cleanup_old_extension_versions(ext)
                )

    def cleanup_old_extension_versions(self, ext: ExtensionData) -> None:
        ext_dir = self.extensions_dir / ext["identifier"]["id"]
        for vers_dir in ext_dir.iterdir():
            if vers_dir.is_dir() and vers_dir.name != ext["version"]:
                for old_file in vers_dir.iterdir():
                    log.info("Removing %s", old_file.name)
                    old_file.unlink()
                vers_dir.rmdir()


def download_file(url: str, dest: Path, *, progress: Progress) -> None:
    dest.parent.mkdir(exist_ok=True, parents=True)
    with progress.task(f"Downloading {dest.name}") as task:
        urllib.request.urlretrieve(url, dest, progress.urllib_callback(task))
    log.info("Downloaded %s", dest.name)


def copy_resource(src: Path, dest: Path) -> None:
    shutil.copy(src, dest)
    file_log.add(dest.resolve())


def copy_template(
    src: Path, dest: Path, /, *, newline: str = os.linesep, **kwargs: str
) -> None:
    data = src.read_text()

    for key, value in kwargs.items():
        old_data = data.replace("{{ " + key + " }}", value)
        if old_data == data:
            log.warning("Key '%s' was not found in %s", key, src.name)
        data = old_data

    start_index = 0
    while (idx := data.find("{{ ", start_index)) != -1:
        end = data.find(" }}", idx)
        start_index = end + 3
        key = data[idx + 3 : end]
        log.warning("Missing key '%s' was found in %s", key, src.name)

    if sys.version_info >= (3, 10):
        dest.write_text(data, newline=newline)
    else:
        # pathlib write_text(newline=...) is not supported in 3.9
        with dest.open("w", newline=newline) as f:
            f.write(data)

    file_log.add(dest.resolve())


def is_linux(platform: str) -> bool:
    return platform.startswith(("linux", "alpine"))


def get_platform_server_download(platform: str) -> str:
    if platform == "darwin-x64":
        platform = "darwin"
    return f"server-{platform}"


def get_platform_client_download(platform: str) -> str:
    if platform in ["win32-x64", "win32-arm64"]:
        return f"{platform}-user"

    if platform in ["darwin-x64", "darwin-arm64"]:
        return platform.removesuffix("-x64")

    return platform


def create_zip(dest: Path, files: Iterable[Path], progress: Progress) -> None:
    log.info("Preparing zip archive")
    with (
        # progress.task(dest.name) as task_id,
        zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zipf,
    ):
        for file in progress.track(files, description=dest.name):
            zipf.write(file, file.relative_to(root))


def multidict(d: dict[str | tuple[str, ...], str]) -> dict[str, str]:
    return {
        k: v
        for keys, v in d.items()
        for k in (keys if isinstance(keys, tuple) else [keys])
    }


def get_tar_mode(file: Path) -> TarFormat:
    for ext in TAR_MODES:
        if file.suffix.endswith(ext):
            return TAR_MODES[ext]

    msg = f"Unknown tar extension: {file}"
    raise AssertionError(msg)


def create_tar(dest: Path, files: Iterable[Path], progress: Progress) -> None:
    mode = get_tar_mode(dest)

    arch_fmt = f"tar.{mode}".strip(".")
    log.info("Preparing %s archive", arch_fmt)
    with tarfile.open(dest, "w:" + mode) as tar:
        for file in progress.track(files, description=dest.name):
            tar.add(file, file.relative_to(root))


class Args:
    code_home: Path | None
    extensions_dir: Path | None
    marketplace_url: str | None
    update_url: str | None

    platform: str
    server_platform: str
    download_server: bool | None
    extensions_only: bool
    download_only: bool

    output_file: Path
    log_level: str


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Download Visual Studio Code extensions and installation files"
    )
    # auto-discovery override options
    parser.add_argument(
        "--code-home",
        type=Path,
        help="Path to Visual Studio Code installation",
    )
    parser.add_argument(
        "--extensions-dir",
        type=Path,
        help="Path to extensions directory",
    )
    parser.add_argument(
        "--marketplace-url",
        "-m",
        help="Marketplace URL to download the extension. Default loads from vscode",
    )
    parser.add_argument(
        "--update-url",
        help="The update url used to download code installers.",
    )

    # download options
    parser.add_argument(
        "--platform",
        "-p",
        choices=["ALL", *TARGET_PLATFORMS],
        default="win32-x64",
        help="Client platform, defaults to win32-x64",
    )
    parser.add_argument(
        "--server-platform",
        "-s",
        choices=["ALL", *TARGET_PLATFORMS],
        default="linux-x64",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--download-server",
        action="store_true",
        dest="download_server",
        help="Download server regardless of remoting extensions",
    )
    parser.add_argument(
        "--no-download-server",
        action="store_false",
        dest="download_server",
        help="Do not download server regardless of remoting extensions",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download, do not archive.",
    )
    parser.set_defaults(download_server=None)
    parser.add_argument(
        "--extensions-only",
        "-x",
        action="store_true",
        help="Download only the extensions",
    )
    # output options
    parser.add_argument(
        "--output-file",
        "-o",
        default="vscode-extensions.zip",
        type=Path,
        help="Name of the archive file. Must be a zip or tar archive",
    )

    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "-vv",
        "--debug",
        action="store_const",
        const="DEBUG",
        dest="log_level",
        help="Enable debug logging",
    )
    log_group.add_argument(
        "-v",
        "--verbose",
        action="store_const",
        const="INFO",
        dest="log_level",
        help="Enable verbose",
    )
    log_group.add_argument(
        "-q",
        "--quiet",
        action="store_const",
        const="ERROR",
        dest="log_level",
        help="Disable output except for errors",
    )
    log_group.add_argument(
        "-qq",
        "--silent",
        action="store_const",
        const="FATAL",
        dest="log_level",
        help="Disable all output, including errors",
    )
    log_group.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "FATAL"],
        help="Set the log level",
        type=str.upper,
    )
    log_group.set_defaults(log_level="WARNING" if rich else "INFO")

    args = parser.parse_args(namespace=Args())

    if args.output_file.suffix not in (".zip", *TAR_EXTENSIONS):
        parser.error("--output-file must be a zip or tar archive")

    return args


def bytes_to_human(size: float) -> str:
    for _unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.1f} {_unit}"


def copy_readme(
    _files: list[Path],
    *,
    commit: str,
    client_dist: str,
    server_dist: str,
    server_home: str,
) -> None:
    outfile = workdir / "README.txt"
    copy_template(
        resources / "README.txt",
        outfile,
        COMMIT=commit,
        CLIENT_DIST=client_dist,
        SERVER_DIST=server_dist,
        SERVER_HOME=server_home,
    )

    file_sizes = {file: file.stat().st_size for file in sorted(_files)}
    with outfile.open("a") as f:
        f.write("\n")
        for file, size in file_sizes.items():
            f.write(f"{bytes_to_human(size):12}{file.relative_to(workdir)}\n")


def copy_script(src: Path, dest: Path, **kwargs: str) -> None:
    copy_template(src, dest, newline="\n", **kwargs)
    dest.chmod(0o755)


def copy_install_script(commit: str, version: str, platform: str) -> None:
    copy_script(
        # TODO externalize templated variables
        resources / "install-server.py",
        workdir / "install-server.py",
        COMMIT=commit,
        PLATFORM=platform,
        VERSION=version,
    )


def main() -> None:
    args = parse_args()

    log.setLevel(args.log_level)

    if args.platform == "ALL" or args.server_platform == "ALL":
        platforms = set(TARGET_PLATFORMS)
    else:
        platforms = {args.platform, args.server_platform}

    product = Product.load(args.code_home)
    extensions = product.load_extensions(args.extensions_dir)
    marketplace = product.marketplace(args.marketplace_url)
    dists = None if args.extensions_only else product.distributions(args.update_url)

    with Progress() as progress, SafeThreadPoolExecutor(10) as executor:
        if marketplace:
            marketplace.download_extensions(extensions, platforms, executor, progress)
            copy_script(
                resources / "install-extensions.py",
                workdir / "install-extensions.py",
            )

        if dists is not None:
            dists.download_client(args.platform, executor, progress)

            should_download_server = args.download_server
            if should_download_server is None:
                should_download_server = extensions.has_remoting_extension()

            if should_download_server:
                dists.download_server(args.server_platform, executor, progress)

                dists.download_cli(args.server_platform, executor, progress)
                if args.server_platform == "ALL":
                    log.warning(
                        "Server platform is set to ALL, not including install script"
                    )
                else:
                    copy_install_script(
                        product.data["commit"],
                        product.data["version"],
                        args.server_platform,
                    )
        executor.shutdown()

        files = sorted(file_log)

        # readme must be last because it calculates the file sizes
        copy_readme(
            files,
            commit=product.data["commit"],
            server_dist=product.get_platform_server_name(args.server_platform),
            client_dist=product.get_platform_client_name(args.platform),
            server_home=product.data["serverDataFolderName"],
        )

    if not args.download_only:
        if args.output_file.suffix == ".zip":
            create_zip(args.output_file, files, progress)
        else:
            create_tar(args.output_file, files, progress)


if __name__ == "__main__":
    try:
        main()
    except AppError as e:
        log.error(str(e))  # noqa: TRY400
        sys.exit(1)
    except KeyboardInterrupt:
        log.critical("Aborted by user")
        sys.exit(130)
    except Exception:  # noqa: BLE001
        log.critical("An unexpected error occurred", exc_info=True)
        sys.exit(1)
