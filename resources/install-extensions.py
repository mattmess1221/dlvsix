#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from dlvsix import ExtensionData

extension_cache = Path(__file__).parent / "extensions"


def get_platform() -> str:
    system = platform.system().lower()
    if system == "windows":
        system = "win32"

    arch = platform.machine().lower()
    if arch in ("amd64", "x86_64"):
        arch = "x64"

    return f"{system}-{arch}"


current_platform = get_platform()


xmlns = {
    "": "http://schemas.microsoft.com/developer/vsx-schema/2011",
    "d": "http://schemas.microsoft.com/developer/vsx-schema/2011",
}


@dataclass(kw_only=True)
class ExtManifest:
    id: str
    version: str
    publisher: str
    platform: str | None

    kinds: set[str]

    @classmethod
    def from_etree(cls, root: ET.Element) -> ExtManifest | None:
        print(root.attrib)
        identity = root.find("Metadata/Identity", xmlns)
        if identity is None:
            return None

        ext_kind_id = "Microsoft.VisualStudio.Code.ExtensionKind"

        prop = root.find(f"Metadata/Properties/Property[@Id='{ext_kind_id}']", xmlns)
        return cls(
            publisher=identity.attrib["Publisher"],
            id=identity.attrib["Id"],
            version=identity.attrib["Version"],
            platform=identity.attrib.get("TargetPlatform"),
            kinds=set(() if prop is None else prop.attrib["Value"].split(",")),
        )


class Extensions:
    def __init__(self, code_home: Path) -> None:
        self.code_home = code_home
        self.extensions_dir = code_home / "extensions"

    @property
    def extensions_file(self) -> Path:
        return self.extensions_dir / "extensions.json"

    @property
    def obsolete_file(self) -> Path:
        return self.extensions_dir / ".obsolete"

    @staticmethod
    def json_dumps(obj: Any) -> str:
        return json.dumps(obj, separators=(",", ":"))

    def __enter__(self) -> Extensions:
        self.obsolete: dict[str, bool] = {}
        with contextlib.suppress(FileNotFoundError):
            self.obsolete = json.loads(self.obsolete_file.read_text())

        self.extensions: list[ExtensionData] = []
        with contextlib.suppress(FileNotFoundError):
            self.extensions = json.loads(self.extensions_file.read_text())

        self.obsolete_dirty = False
        self.extensions_dirty = False
        return self

    def __exit__(self, *_: Any) -> None:
        if self.obsolete_dirty:
            self.obsolete_file.write_text(self.json_dumps(self.obsolete))

        if self.extensions_dirty:
            self.extensions_file.write_text(self.json_dumps(self.extensions))

    def get_extension(self, ext_id: str, version: str) -> ExtensionData | None:
        for ext in self.extensions:
            if ext["identifier"]["id"].lower() == ext_id and (
                not self.is_obsolete(ext) or ext["version"] == version
            ):
                return ext
        return None

    def is_obsolete(self, ext: ExtensionData) -> bool:
        ext_id = f"{ext['identifier']['id']}-{ext['version']}".lower()
        return self.obsolete.get(ext_id, False)

    def extract_vsix(self, z: zipfile.ZipFile, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with (
            z.open("extension.vsixmanifest") as f,
            (dest / ".vsixmanifest").open("wb") as f2,
        ):
            shutil.copyfileobj(f, f2)

        for entry in z.namelist():
            if not entry.startswith("extension/"):
                continue
            target = os.path.relpath(entry, "extension")
            target = dest / target
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with z.open(entry) as f, (target).open("wb") as f2:
                    shutil.copyfileobj(f, f2)
            except OSError as e:
                print(f"{type(e).__name__}: {e}")

    def install_extension(self, vsix: Path, *, install_server: bool = False) -> None:
        with zipfile.ZipFile(vsix) as z:
            with z.open("extension.vsixmanifest") as f:
                mft = ExtManifest.from_etree(ET.fromstring(f.read()))

            if mft is None:
                print(f"warning: Invalid extension manifest for {vsix.name}")
                return

            if install_server and "workspace" not in mft.kinds:
                return

            if mft.platform is None or mft.platform == current_platform:
                pub = mft.publisher
                name = mft.id
                vers = mft.version

                location = f"{pub}.{name}-{vers}".lower()

                installed = self.get_extension(f"{pub}.{name}".lower(), vers)

                if installed is not None and installed["version"] == vers:
                    return

                print(f"Installing extension: {location}")

                self.extract_vsix(z, self.extensions_dir / location)
                self.add_extension(pub, name, vers, location)

                if installed is not None:
                    self.add_obsolete(installed)

    def add_extension(
        self, publisher: str, name: str, version: str, location: str
    ) -> None:
        abs_location = self.extensions_dir.resolve() / location
        scheme, _, path, *__ = urllib.parse.urlparse(abs_location.as_uri())

        self.extensions.append(
            {
                "identifier": {
                    "id": f"{publisher}.{name}".lower(),
                },
                "version": version,
                "location": {
                    "$mid": 1,
                    "path": path,
                    "scheme": scheme,
                },
                "relativeLocation": location,
                "metadata": {
                    "isApplicationScoped": False,
                    "isMachineScoped": False,
                    "isBuiltin": False,
                    "installedTimestamp": int(time.time()),
                    "pinned": True,
                    "source": "vsix",
                },
            }
        )
        self.extensions_dirty = True

    def add_obsolete(self, ext: ExtensionData) -> None:
        ext_id = f"{ext['identifier']['id']}-{ext['version']}".lower()
        self.obsolete[ext_id] = True
        self.obsolete_dirty = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-server", action="store_true")
    parser.add_argument("--code-home", type=Path)
    args = parser.parse_args()
    if args.code_home is not None:
        code_home = args.code_home.resolve()
    else:
        code_path = ".vscode-server" if args.install_server else ".vscode"
        code_home = Path.home() / code_path

    print("Installing extensions to:", code_home)

    with Extensions(code_home) as exts:
        for file in extension_cache.iterdir():
            if file.suffix == ".vsix":
                exts.install_extension(file, install_server=args.install_server)


if __name__ == "__main__":
    main()
