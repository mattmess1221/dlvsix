#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "rich",
#     "rich-argparse",
# ]
# ///
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
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
import urllib.request
import zipfile
from collections.abc import Generator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    DownloadColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.progress import Progress as _Progress
from rich_argparse import RichHelpFormatter

if t.TYPE_CHECKING:
    P = t.ParamSpec("P")
    Ts = t.TypeVarTuple("Ts")
    R = t.TypeVar("R", covariant=True)

root = Path(__file__).parent.resolve()
resources = root / "resources"
workdir = root / "vscode-extensions"


IS_WSL = bool(os.getenv("WSL_DISTRO_NAME"))


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
    (".tar",): "",
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
    def __exit__(self, /, *exc_info: t.Any) -> None:
        if exc_info[0]:
            if isinstance(exc_info[1], KeyboardInterrupt):
                log.critical("Caught KeyboardInterrupt, shutting down")
            else:
                log.critical("Caught exception, shutting down", exc_info=exc_info)
            self.shutdown(wait=False, cancel_futures=True)

        super().__exit__(*exc_info)


class CountingIO(t.IO[bytes]):
    def __init__(self, raw: t.IO[bytes], callback: t.Callable[[int], None]) -> None:
        self.raw = raw
        self.callback = callback

    def read(self, n: int = -1) -> bytes:
        data = self.raw.read(n)
        self.callback(len(data))
        return data

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self.raw, name)


class Progress:
    def __init__(self) -> None:
        self.progress = _Progress(
            TextColumn("[progress.description]{task.description:80}"),
            BarColumn(),
            DownloadColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        )

    def __enter__(self) -> t.Self:
        self.progress.start()
        return self

    def __exit__(self, *_: t.Any) -> None:
        self.progress.stop()

    @contextlib.contextmanager
    def task(
        self, description: str, *, total: int | None = None
    ) -> t.Generator[ProgressTask]:
        task = self.progress.add_task(description, total=total)
        try:
            yield ProgressTask(self, task)
        finally:
            self.progress.remove_task(task)

    def update(
        self,
        task: TaskID,
        *,
        advance: int | None = None,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        self.progress.update(task, advance=advance, completed=completed, total=total)


class ProgressTask:
    def __init__(self, progress: Progress, task: TaskID) -> None:
        self.progress = progress
        self.task = task

    def urllib_callback(self) -> t.Callable[[int, int, int], None] | None:
        return lambda blocknum, bs, size: self.progress.update(
            self.task, completed=blocknum * bs, total=size
        )

    def wrap_file(self, srcobj: t.IO[bytes]) -> t.IO[bytes]:
        return CountingIO(srcobj, lambda n: self.progress.update(self.task, advance=n))


class ColorFormatter(logging.Formatter):
    colors: t.ClassVar = {
        "debug": GRAY,
        "info": BLUE,
        "warning": YELLOW,
        "error": RED,
        "critical": BOLD + ITALIC + DARK_RED + BG_YELLOW,
    }

    if sys.version_info >= (3, 13):

        @t.override
        def formatException(self, ei: t.Any) -> str:
            sio = io.StringIO()
            traceback.print_exception(*ei, file=sio, colorize=sys.stderr.isatty())  # type: ignore
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

    handler = RichHandler(show_time=False, show_path=False)

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


if t.TYPE_CHECKING:

    class ProgressFn(t.Protocol[t.Unpack[Ts]]):
        __name__: str

        def __call__(
            self,
            *args: t.Unpack[Ts],
            progress: Progress,
        ) -> None: ...


@dataclass()
class App:
    progress: Progress
    executor: ThreadPoolExecutor
    dry_run: bool = False

    @classmethod
    @contextlib.contextmanager
    def create(cls, *, dry_run: bool = False) -> t.Generator[t.Self]:
        with Progress() as progress, SafeThreadPoolExecutor(10) as executor:
            yield cls(progress=progress, executor=executor, dry_run=dry_run)

    def run_fn(self, fn: ProgressFn[t.Unpack[Ts]], *args: t.Unpack[Ts]) -> None:
        """Execute the provided function `fn` with the given arguments.

        If `self.dry_run` is True, the function will not be executed, and a log
        message will be printed instead. Otherwise, the function will be executed
        with the provided arguments and the `progress` object.
        """
        if self.dry_run:
            import inspect

            bound = inspect.signature(fn).bind_partial(*args).arguments
            log.info(
                "%s(%s)",
                fn.__name__,
                "\n\t"
                + ",\n\t".join(f"{key}={value!r}" for key, value in bound.items())
                + "\n",
            )
        else:
            fn(*args, progress=self.progress)

    def submit(self, fn: ProgressFn[t.Unpack[Ts]], *args: t.Unpack[Ts]) -> Future[None]:
        return self.executor.submit(self.run_fn, fn, *args)


@dataclass()
class Product:
    app: App
    code_home: Path
    data: ProductJson

    def __post_init__(self) -> None:
        log.debug("Detected vscode version: %s", self.data["version"])

    @classmethod
    def load(cls, app: App, code_home: Path | None) -> t.Self:
        if code_home is None:
            code_home = get_vscode_home()
        product_json = code_home / PRODUCT_JSON_PATH
        log.debug("Loading product json from %s", product_json)
        try:
            with product_json.open() as f:
                return cls(app, code_home, json.load(f))
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
            return Marketplace(self.app, marketplace_url)

        msg = (
            "Unable to load marketplace service url from product.json"
            " Supply it via --marketplace-url"
        )
        raise AppError(msg)

    def distributions(
        self,
        update_url: str | None,
    ) -> Distributions:
        if self.data["applicationName"] == "codium":
            msg = (
                "Fetching of VSCodium distributions is not supported. Its API only"
                " returns the latest version and does not support fetching the"
                " currently installed version, which may not be the latest."
                "\n\n"
                "To disable fetching of distributions, pass the --no-download-dists"
                " option."
            )
            raise AppError(msg)

        if update_url is None:
            update_url = self.data.get("updateUrl")

        if update_url is not None:
            log.debug("Using update url: %s", update_url)
            return Distributions(self, update_url)

        msg = (
            "Unable to load update url from product.json. Supply it via --update-url or"
            " disable distributions via --no-download-dists"
        )
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

    def load_extensions(
        self, extensions_dir: Path | None, *, ignored: set[str]
    ) -> Extensions:
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

        # Exclude explicitly ignored extensions
        data = [
            ext for ext in data if ext["identifier"]["id"].casefold() not in ignored
        ]

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

    @classmethod
    def read_ignored_extensions(cls) -> list[str]:
        ignorefile = root / ".vsixignore"
        if ignorefile.exists():
            with ignorefile.open() as f:
                return [
                    ext_id.casefold()
                    for line in f
                    if (ext_id := line.split("#")[0].strip())
                ]
        return []


class ApiVersion(t.TypedDict):
    url: str
    name: str
    version: str
    productVersion: str
    hash: str
    timestamp: int
    sha256hash: str
    supportsFastUpdate: bool


class Distributions:
    def __init__(self, product: Product, update_url: str) -> None:
        self.app = product.app
        self.product = product
        self.update_url = update_url

        self.dist_dir = workdir / "dist" / self.product.data["commit"]
        self.dist_dir.mkdir(exist_ok=True, parents=True)

    def download_dist(self, dist: str, dest: Path) -> None:
        if dest.exists():
            file_log.add(dest.resolve())
            return

        # Microsoft's update API is basically undocumented
        # https://stackoverflow.com/a/69810842/2351110
        commit = self.product.data["commit"]
        api_url = f"{self.update_url}/api/versions/commit:{commit}/{dist}/stable"

        with urllib.request.urlopen(api_url) as resp:
            data: ApiVersion = json.load(resp)

        self.app.submit(
            download_file,
            data["url"],
            dest,
            data["sha256hash"],
        )

    def download_client(self, target_platform: str) -> None:
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
            )

    def download_server(self, target_platform: str) -> None:
        if target_platform.startswith("alpine"):
            target_platform = target_platform.replace("alpine", "linux")

        for platform in self.get_dist_platforms(target_platform):
            if platform.startswith("alpine"):
                continue
            name = self.product.get_platform_server_name(platform)
            self.download_dist(
                get_platform_server_download(platform),
                self.dist_dir / name,
            )

    def download_cli(self, target_platform: str) -> None:
        for platform in self.get_dist_platforms(target_platform):
            ext = "zip" if platform.startswith("win32") else "tar.gz"
            self.download_dist(
                f"cli-{platform}",
                self.dist_dir / f"vscode-cli-{platform}-cli.{ext}",
            )

    @staticmethod
    def get_dist_platforms(target_platform: str) -> set[str]:
        if target_platform == "ALL":
            return set(TARGET_PLATFORMS)

        return {target_platform}


class Marketplace:
    def __init__(self, app: App, service_url: str) -> None:
        self.app = app
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

    def download_extensions(self, extensions: Extensions, platforms: set[str]) -> None:
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
                fut = self.app.submit(download_file, url, base / file)
                fut.add_done_callback(
                    lambda fut, ext=ext: fut.cancelled()
                    or self.cleanup_old_extension_versions(ext)
                )

    def cleanup_old_extension_versions(self, ext: ExtensionData) -> None:
        ext_dir = self.extensions_dir / ext["identifier"]["id"]
        if not ext_dir.exists():
            return
        for vers_dir in ext_dir.iterdir():
            if vers_dir.is_dir() and vers_dir.name != ext["version"]:
                for old_file in vers_dir.iterdir():
                    if not self.app.dry_run:
                        log.info("Removing %s", old_file.name)
                        old_file.unlink()
                    else:
                        log.info("Would remove %s", old_file)

                if not self.app.dry_run:
                    vers_dir.rmdir()
                else:
                    log.info("Would remove %s", vers_dir)


def verify_sha256_hash(filename: Path, sha256hash: str) -> bool:
    log.debug("Verifying hash of %s", filename.name)
    with filename.open("br") as f:
        actual_hash = hashlib.sha256(f.read()).hexdigest()

    if sha256hash != actual_hash:
        log.error("SHA256 Verify failed for %s!", filename.name)
        filename.unlink()
        return False
    return True


def exc_logger(f: t.Callable[P, R]) -> t.Callable[P, R]:
    @functools.wraps(f)
    def decorator(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return f(*args, **kwargs)
        except Exception:
            log.exception("Unhandled exception during %s()", f.__name__)
            raise

    return decorator


@exc_logger
def download_file(
    url: str,
    dest: Path,
    sha256hash: str | None = None,
    *,
    progress: Progress,
) -> None:
    try:
        dest.parent.mkdir(exist_ok=True, parents=True)
        with progress.task(f"Downloading {dest.name}") as task:
            urllib.request.urlretrieve(url, dest, task.urllib_callback())
    except urllib.request.HTTPError as e:
        log.exception("Download failed with status %s for url: %s", e.status, url)
    else:
        if not sha256hash or verify_sha256_hash(dest, sha256hash):
            log.info("Downloaded %s", dest.name)
            file_log.add(dest.resolve())


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


def count_total_bytes(paths: Iterable[Path]) -> int:
    return sum(path.stat().st_size for path in paths if path.is_file())


def create_zip(dest: Path, files: Iterable[Path], *, progress: Progress) -> None:
    log.info("Preparing zip archive")
    with (
        progress.task(dest.name, total=count_total_bytes(files)) as zip_task,
        zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zipf,
    ):
        for file in files:
            with progress.task("⨽" + file.name, total=file.stat().st_size) as file_task:
                arcname = str(file.relative_to(root))
                with file.open("rb") as srcobj, zipf.open(arcname, "w") as destobj:
                    srcobj = file_task.wrap_file(srcobj)
                    srcobj = zip_task.wrap_file(srcobj)
                    shutil.copyfileobj(srcobj, destobj)


def multidict(d: dict[str | tuple[str, ...], str]) -> dict[str, str]:
    return {
        k: v
        for keys, v in d.items()
        for k in (keys if isinstance(keys, tuple) else [keys])
    }


def get_tar_mode(file: Path) -> TarFormat:
    for ext in TAR_MODES:
        if file.name.endswith(ext):
            return TAR_MODES[ext]

    msg = f"Unknown tar extension: {file}"
    raise AssertionError(msg)


def create_tar(dest: Path, files: Iterable[Path], *, progress: Progress) -> None:
    mode = get_tar_mode(dest)

    arch_fmt = f"tar.{mode}".strip(".")
    log.info("Preparing %s archive", arch_fmt)
    with (
        progress.task(dest.name, total=count_total_bytes(files)) as tar_task,
        tarfile.open(dest, "w:" + mode) as tar,
    ):
        for file in files:
            with progress.task("⨽" + file.name, total=file.stat().st_size) as file_task:
                tarinfo = tar.gettarinfo(file, str(file.relative_to(root)))
                with file.open("rb") as srcobj:
                    srcobj = file_task.wrap_file(srcobj)
                    srcobj = tar_task.wrap_file(srcobj)

                    tar.addfile(tarinfo, srcobj)


class Args:
    code_home: Path | None
    extensions_dir: Path | None
    marketplace_url: str | None
    update_url: str | None

    platform: str
    server_platform: str
    download_server: bool | None
    download_client: bool
    download_extensions: bool
    download_dists: bool
    ignored_extensions: list[str]
    dry_run: bool

    output_file: Path | None
    log_level: str

    def __repr__(self) -> str:
        return f"Args({', '.join(f'{k}={v!r}' for k, v in vars(self).items())})"


def parse_args() -> Args:
    parser = argparse.ArgumentParser(
        description="Download Visual Studio Code extensions and installation files",
        formatter_class=RichHelpFormatter,
    )
    base_options = parser.add_argument_group(
        "Product Options",
        description=(
            "These options are auto-detected by default. Use them to override the"
            " defaults."
        ),
    )
    # auto-discovery override options
    base_options.add_argument(
        "--code-home",
        type=Path,
        help="Path to Visual Studio Code installation",
    )
    base_options.add_argument(
        "--extensions-dir",
        type=Path,
        help="Path to extensions directory",
    )
    base_options.add_argument(
        "--marketplace-url",
        "-m",
        help="Marketplace URL to download the extension. Default loads from vscode",
    )
    base_options.add_argument(
        "--update-url",
        help="The update url used to download code installers.",
    )

    # download options
    ext_group = parser.add_argument_group(
        "Extension Options",
        description="Options for downloading and selecting extensions to pack.",
    )
    ext_group.add_argument(
        "--platform",
        "-p",
        choices=["ALL", *TARGET_PLATFORMS],
        default="win32-x64",
        help="Client platform, defaults to win32-x64",
    )
    ext_group.add_argument(
        "--server-platform",
        "-s",
        choices=["ALL", *TARGET_PLATFORMS],
        default="linux-x64",
        help=argparse.SUPPRESS,
    )
    ext_group.add_argument(
        "--no-download-server",
        action="store_false",
        dest="download_server",
        default=None,
        help="Do not download server regardless of remoting extensions",
    )
    ext_group.add_argument(
        "--no-download-client",
        action="store_false",
        dest="download_client",
        help="Do not download the client.",
    )
    ext_group.add_argument(
        "--no-download-dists",
        "--extensions-only",
        "-x",
        action="store_false",
        dest="download_dists",
        help="Do not download any vscode distributions.",
    )
    ext_group.add_argument(
        "--ignore-extension",
        "-i",
        dest="ignored_extensions",
        metavar="EXTENSION_ID",
        default=[],
        type=str.casefold,
        action="append",
        help=(
            "Ignore an extension by id. Can be used multiple times, case-insensitive."
            " Can also be set in the file .vsixignore, one entry per line."
        ),
    )
    ext_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not download anything, only print what would be downloaded",
    )
    # output options
    output_group = parser.add_argument_group(
        "Output Options",
        description="Options for output files.",
    ).add_mutually_exclusive_group()
    output_group.add_argument(
        "--output-file",
        "-o",
        default="vscode-extensions.zip",
        type=Path,
        help="Name of the archive file. Must be a zip or tar archive",
    )
    output_group.add_argument(
        "--no-output-file",
        "--download-only",
        action="store_const",
        const=None,
        dest="output_file",
        help="Disable creation of final archive file",
    )

    log_group = parser.add_argument_group(
        "Logging Options",
        description=(
            "Set the log level. The default is INFO, which is verbose. If rich "
            "is installed, the default will be WARNING."
        ),
    ).add_mutually_exclusive_group()
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
    log_group.set_defaults(log_level="INFO")

    args = parser.parse_args(namespace=Args())

    valid_extensions = (".zip", *TAR_EXTENSIONS)

    if args.output_file and not args.output_file.name.endswith(valid_extensions):
        parser.error("--output-file must be a zip or tar archive")

    return args


def bytes_to_human(size: float) -> str:
    _unit = "B"
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

    with App.create(dry_run=args.dry_run) as app:
        product = Product.load(app, args.code_home)

        ignored = {
            *(args.ignored_extensions or []),
            *Extensions.read_ignored_extensions(),
        }

        extensions = product.load_extensions(args.extensions_dir, ignored=ignored)
        marketplace = product.marketplace(args.marketplace_url)

        if marketplace:
            marketplace.download_extensions(extensions, platforms)
            copy_script(
                resources / "install-extensions.py",
                workdir / "install-extensions.py",
            )

        if args.download_dists:
            dists = product.distributions(args.update_url)
            if args.download_client:
                dists.download_client(args.platform)

            should_download_server = args.download_server
            if should_download_server is None:
                should_download_server = extensions.has_remoting_extension()

            if should_download_server:
                dists.download_server(args.server_platform)

                dists.download_cli(args.server_platform)
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
        app.executor.shutdown()

        files = sorted(file_log)

        # readme must be last because it calculates the file sizes
        copy_readme(
            files,
            commit=product.data["commit"],
            server_dist=product.get_platform_server_name(args.server_platform),
            client_dist=product.get_platform_client_name(args.platform),
            server_home=product.data["serverDataFolderName"],
        )

        if args.output_file:
            if args.output_file.suffix == ".zip":
                app.run_fn(create_zip, args.output_file, files)
            else:
                app.run_fn(create_tar, args.output_file, files)


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
