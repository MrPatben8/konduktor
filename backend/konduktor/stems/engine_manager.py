"""The stem engine and its weights on THIS machine: what is installed, getting
it, getting rid of it.

The engine is a separate frozen program, released on its own channel
(`engine-v<version>`; see `engine/` and `.github/workflows/engine.yml`) and
named by `engine.json` — the version this app build needs. The weights are
pinned by revision and SHA-256 in `weights.json` (a copy of
`engine/weights.json`; a test keeps them equal) and fetched from Hugging Face,
never re-hosted.

Layout under `root` — app-data on macOS; on Windows a SHORT root,
`%LOCALAPPDATA%\\Konduktor\\engine`, because torch's tree is deep and Windows
paths stop at 260 characters:

    <root>/<version>/<target>/konduktor-engine/konduktor-engine[.exe]
    <root>/installed.json            what is installed, written LAST
    <root>/weights/<revision>/...    the four .safetensors + htdemucs_ft.yaml
    <root>/downloads/                resumable .part files

An install is verified three times before it counts: each part's SHA-256, the
whole archive's, and — once unpacked into a temporary folder — the engine
actually starting and reporting the expected version. Only then is it moved
into place and `installed.json` written, so a half-finished or corrupt install
can never look like a working one. One engine is installed at a time (on
Windows the CUDA engine also runs on the CPU, so it replaces the CPU one).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import paths
from . import download

_HERE = Path(__file__).resolve().parent
CONFIG = json.loads((_HERE / "engine.json").read_text())
WEIGHTS = json.loads((_HERE / "weights.json").read_text())

TARGETS = ("macos-arm64", "windows-x64-cpu", "windows-x64-cuda")


class EngineError(Exception):
    """User-facing."""


def default_root() -> Path:
    override = os.environ.get("KONDUKTOR_DATA_DIR")
    if override:
        return Path(override) / "engine"
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Konduktor" / "engine"
    return paths.app_data_dir() / "engine"


def base_target() -> str | None:
    """The engine this computer can run at all; None = no engine for it
    (Intel Macs, Linux)."""
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x64-cpu"
    return None


@dataclass(frozen=True)
class NvidiaGpu:
    name: str
    driver: str
    capability: float


def detect_nvidia() -> list[NvidiaGpu]:
    """NVIDIA cards per `nvidia-smi` (installed with the driver). Empty when
    there is none, or no driver — the case for every Mac."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3:
            try:
                gpus.append(NvidiaGpu(parts[0], parts[1], float(parts[2])))
            except ValueError:
                continue
    return gpus


def cuda_suitable(gpus: list[NvidiaGpu], config: dict = CONFIG) -> NvidiaGpu | None:
    """The first card the CUDA engine can use: a new enough driver and compute
    capability. Offering it to anyone else would be offering gigabytes for an
    engine that falls back to the CPU."""
    floor = config.get("cuda", {})
    for g in gpus:
        try:
            major = int(g.driver.split(".")[0])
        except ValueError:
            continue
        if major >= floor.get("min_driver", 0) and g.capability >= floor.get("min_capability", 0):
            return g
    return None


class EngineManager:
    def __init__(self, root: Path | None = None, config: dict | None = None,
                 weights: dict | None = None, target: str | None = "auto") -> None:
        self.root = Path(root) if root else default_root()
        self.config = config or CONFIG
        self.weights = weights or WEIGHTS
        self._base_target = base_target() if target == "auto" else target
        self._manifest: dict | None = None

    # ---- where things are --------------------------------------------------
    @property
    def version(self) -> str:
        return self.config["version"]

    @property
    def base_target(self) -> str | None:
        return self._base_target

    def _installed_record(self) -> dict | None:
        return paths.read_json(self.root / "installed.json", None)

    def installed(self) -> dict | None:
        """The installed engine, if it is this app's version and still there."""
        rec = self._installed_record()
        if not rec or rec.get("version") != self.version:
            return None
        exe = Path(rec.get("executable", ""))
        return rec if exe.is_file() else None

    def executable(self) -> Path | None:
        rec = self.installed()
        return Path(rec["executable"]) if rec else None

    def weights_dir(self) -> Path:
        return self.root / "weights" / self.weights["revision"]

    def weights_installed(self) -> bool:
        """Present at the right sizes. (Hashes were checked when they arrived;
        re-hashing 337 MB on every status call is not worth it.)"""
        d = self.weights_dir()
        return all((d / f["name"]).is_file() and (d / f["name"]).stat().st_size == f["size"]
                   for f in self.weights["files"])

    def weights_size(self) -> int:
        return sum(f["size"] for f in self.weights["files"])

    # ---- manifest ------------------------------------------------------------
    def manifest(self, *, fetch: bool = True) -> dict | None:
        """The release's manifest (each target's parts and hashes). Cached on
        disk: once fetched, an install or side-load needs no network for it."""
        if self._manifest is not None:
            return self._manifest
        cached = self.root / f"manifest-{self.version}.json"
        data = paths.read_json(cached, None)
        if data is None and fetch:
            url = self.config["manifest_url"].format(version=self.version)
            import urllib.request

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Konduktor"})
                with urllib.request.urlopen(req, timeout=20, context=download.ssl_context()) as r:
                    data = json.loads(r.read())
            except (OSError, ValueError):
                return None
            if data.get("version") != self.version:
                return None
            self.root.mkdir(parents=True, exist_ok=True)
            paths.write_json(cached, data)
        self._manifest = data
        return data

    def target_info(self, target: str) -> dict | None:
        m = self.manifest()
        return (m or {}).get("targets", {}).get(target)

    # ---- status ----------------------------------------------------------------
    def status(self, *, network: bool = True) -> dict:
        installed = self.installed()
        gpus = detect_nvidia()
        cuda = cuda_suitable(gpus, self.config)
        offered = [self.base_target] if self.base_target else []
        if self.base_target == "windows-x64-cpu" and cuda is not None:
            offered.append("windows-x64-cuda")
        manifest = self.manifest(fetch=network) if offered else None
        sizes = {t: (manifest or {}).get("targets", {}).get(t, {}).get("size") for t in offered}
        return {
            "supported": self.base_target is not None,
            "required_version": self.version,
            "installed": ({k: installed[k] for k in ("version", "target", "size")} if installed else None),
            "offered_targets": offered,
            "download_sizes": sizes,
            "manifest_available": manifest is not None,
            "nvidia": ({"name": cuda.name, "driver": cuda.driver, "capability": cuda.capability} if cuda else None),
            "weights": {"installed": self.weights_installed(), "size": self.weights_size(),
                        "revision": self.weights["revision"]},
            "root": str(self.root),
        }

    def ready(self) -> bool:
        return self.executable() is not None and self.weights_installed()

    # ---- installing the engine ---------------------------------------------------
    def install_engine(self, target: str | None = None, *,
                       on_bytes: Callable[[int], None] = lambda n: None,
                       cancelled: Callable[[], bool] = lambda: False) -> dict:
        target = target or self.base_target
        if target is None or target not in TARGETS:
            raise EngineError("There is no stem engine for this computer")
        info = self.target_info(target)
        if info is None:
            raise EngineError("Could not reach the engine download — check the internet connection")
        downloads = self.root / "downloads"
        ctx = download.ssl_context()
        for part in info["parts"]:
            url = self.config["asset_url"].format(version=self.version, name=part["name"])
            download.fetch(url, downloads / part["name"], sha256=part["sha256"], size=part["size"],
                           on_bytes=on_bytes, cancelled=cancelled, context=ctx)
        archive = self._join_parts(info, downloads)
        rec = self._install_archive(archive, target, info)
        for part in info["parts"]:
            (downloads / part["name"]).unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        return rec

    def _join_parts(self, info: dict, folder: Path) -> Path:
        parts = [folder / p["name"] for p in info["parts"]]
        if len(parts) == 1:
            archive = parts[0]
        else:
            archive = folder / info["archive"]
            with open(archive, "wb") as out:
                for p in parts:
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, out, 1 << 20)
        if download.sha256_file(archive) != info["sha256"]:
            raise EngineError("The engine download is damaged (checksum mismatch); try again")
        return archive

    def _install_archive(self, archive: Path, target: str, info: dict) -> dict:
        """Unpack, prove the engine starts, THEN move it into place."""
        final = self.root / self.version / target
        staging = self.root / self.version / f".{target}.installing"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(staging, filter="tar")
            exe = staging / "konduktor-engine" / ("konduktor-engine.exe" if sys.platform == "win32" else "konduktor-engine")
            if not exe.is_file():
                raise EngineError("The engine archive does not contain the engine")
            reported = probe(exe)
            if reported.get("version") != self.version:
                raise EngineError(f"The engine reports version {reported.get('version')!r}, "
                                  f"this app needs {self.version}")
            shutil.rmtree(final, ignore_errors=True)
            os.replace(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        rec = {"version": self.version, "target": target, "size": info.get("unpacked_size"),
               "executable": str(final / "konduktor-engine" / exe.name), "info": reported}
        paths.write_json(self.root / "installed.json", rec)
        self._remove_others(keep=final)
        return rec

    def _remove_others(self, keep: Path) -> None:
        """One engine at a time: other targets and older versions go."""
        for version_dir in self.root.iterdir():
            if not version_dir.is_dir() or version_dir.name in ("weights", "downloads"):
                continue
            for target_dir in version_dir.iterdir():
                if target_dir != keep and target_dir.is_dir():
                    shutil.rmtree(target_dir, ignore_errors=True)
            if not any(version_dir.iterdir()):
                version_dir.rmdir()

    # ---- installing the weights ------------------------------------------------------
    def install_weights(self, *, on_bytes: Callable[[int], None] = lambda n: None,
                        cancelled: Callable[[], bool] = lambda: False) -> None:
        folder = self.weights_dir()
        ctx = download.ssl_context()
        for f in self.weights["files"]:
            url = self.weights["url"].format(repo=self.weights["repo"], revision=self.weights["revision"],
                                             name=f["name"])
            download.fetch(url, folder / f["name"], sha256=f["sha256"], size=f["size"],
                           on_bytes=on_bytes, cancelled=cancelled, context=ctx)

    # ---- side-loading (offline machines) ------------------------------------------
    def side_load_engine(self, path: Path | str, target: str | None = None) -> dict:
        """Install from an engine archive the user already has — the `.tar.gz`,
        or a folder holding all of its parts. Its hash must match the release
        manifest (fetched now, or cached from an earlier look)."""
        target = target or self.base_target
        info = self.target_info(target) if target else None
        if info is None:
            raise EngineError("The engine's checksums are not known yet — connect once, or open the "
                              "Stems panel while online, so the file can be verified")
        src = Path(path)
        if src.is_dir():
            archive = self._join_parts(info, src)
        else:
            if download.sha256_file(src) != info["sha256"]:
                raise EngineError("That file is not the engine this app needs (checksum mismatch)")
            archive = src
        return self._install_archive(archive, target, info)

    def side_load_weights(self, folder: Path | str) -> None:
        """Copy the weights from a folder the user supplies, each file verified."""
        src = Path(folder)
        dest = self.weights_dir()
        dest.mkdir(parents=True, exist_ok=True)
        for f in self.weights["files"]:
            p = src / f["name"]
            if not p.is_file() or download.sha256_file(p) != f["sha256"]:
                raise EngineError(f"{f['name']} is missing from that folder or is not the right file")
        for f in self.weights["files"]:
            shutil.copy2(src / f["name"], dest / f["name"])

    # ---- removal -------------------------------------------------------------------
    def remove(self) -> None:
        """The engine, its weights and any unfinished downloads."""
        (self.root / "installed.json").unlink(missing_ok=True)
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                elif child.name != "installed.json":
                    child.unlink(missing_ok=True)


_DEFAULT: "EngineManager | None" = None


def default_manager() -> EngineManager:
    """The app's one manager (the routes and the batch share it)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = EngineManager()
    return _DEFAULT


def probe(executable: Path, timeout: float = 120) -> dict:
    """Run `engine info` — proves the unpacked engine actually starts."""
    try:
        out = subprocess.run([str(executable), "info"], capture_output=True, text=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as ex:
        raise EngineError(f"The engine would not start: {ex}") from ex
    for line in out.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("event") == "info":
            return msg
    raise EngineError("The engine started but did not identify itself")
