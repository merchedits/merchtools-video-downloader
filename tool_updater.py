from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PYPI_YT_DLP_URL = "https://pypi.org/pypi/yt-dlp/json"
PYPI_YT_DLP_EJS_URL_TEMPLATE = "https://pypi.org/pypi/yt-dlp-ejs/{version}/json"
PYPI_YT_DLP_EJS_LATEST_URL = "https://pypi.org/pypi/yt-dlp-ejs/json"
TWITCH_DOWNLOADER_LATEST_RELEASE_URL = "https://api.github.com/repos/lay295/TwitchDownloader/releases/latest"

MANAGED_PYTHON_TOP_LEVEL_PACKAGES = frozenset({"yt_dlp", "yt_dlp_ejs"})
TOOL_UPDATE_CHECK_INTERVAL_SECONDS = 900  # 15 minutes for automatic background checks
DOWNLOAD_CHUNK_SIZE = 256 * 1024
STATE_FILENAME = "state.json"


def user_data_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "MerchTools" / "Video Downloader"
    return Path.home() / "AppData" / "Roaming" / "MerchTools" / "Video Downloader"


def managed_tools_dir(data_dir: Path | None = None) -> Path:
    return (data_dir or user_data_dir()) / "managed_tools"


def managed_tools_state_path(data_dir: Path | None = None) -> Path:
    return managed_tools_dir(data_dir) / STATE_FILENAME


def version_key(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(parts or [0])


def is_newer_version(current_version: str, candidate_version: str) -> bool:
    return version_key(candidate_version) > version_key(current_version)


def subprocess_window_options() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def python_satisfies_requires_python(
    requires_python: str | None,
    current_version: tuple[int, int] | None = None,
) -> bool:
    if not requires_python or not requires_python.strip():
        return True
    actual = current_version or (sys.version_info.major, sys.version_info.minor)
    for raw_clause in requires_python.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        match = re.match(r"^(>=|<=|!=|==|>|<)\s*(\d+(?:\.\d+)*)(?:\.\*)?$", clause)
        if not match:
            continue
        operator, ver_text = match.groups()
        parts = [int(p) for p in ver_text.split(".")]
        target = (parts[0], parts[1] if len(parts) > 1 else 0)
        if operator == ">=" and not (actual >= target):
            return False
        if operator == ">" and not (actual > target):
            return False
        if operator == "<=" and not (actual <= target):
            return False
        if operator == "<" and not (actual < target):
            return False
        if operator == "==" and actual != target:
            return False
        if operator == "!=" and actual == target:
            return False
    return True


def parse_required_ejs_version(requires_dist: list[str] | None) -> str | None:
    if not requires_dist:
        return None
    fallback: str | None = None
    for requirement in requires_dist:
        if not isinstance(requirement, str):
            continue
        text = requirement.strip()
        match = re.match(r"^yt-dlp-ejs\s*(==|>=)\s*([0-9][0-9A-Za-z._-]*)", text, flags=re.IGNORECASE)
        if not match:
            continue
        operator, version = match.groups()
        if operator == "==" and 'extra == "default"' in text:
            return version
        if operator == "==" and fallback is None:
            fallback = version
        elif fallback is None:
            fallback = version
    return fallback


def load_managed_tools_state(data_dir: Path | None = None) -> dict:
    path = managed_tools_state_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_managed_tools_state(state: dict, data_dir: Path | None = None) -> None:
    path = managed_tools_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{STATE_FILENAME}.tmp.{os.getpid()}.{time.time_ns()}"
    tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


class ManagedPackageMetaPathFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder that overrides bundled yt_dlp / yt_dlp_ejs even in frozen PyInstaller apps."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir

    def set_root_dir(self, root_dir: Path | None) -> None:
        self.root_dir = root_dir

    def find_spec(self, fullname: str, path=None, target=None):
        root_dir = self.root_dir
        if root_dir is None:
            return None
        top_level = fullname.split(".", 1)[0]
        if top_level not in MANAGED_PYTHON_TOP_LEVEL_PACKAGES:
            return None
        search_paths = [str(root_dir)] if path is None else list(path)
        return importlib.machinery.PathFinder.find_spec(fullname, search_paths, target)

    def find_distributions(self, context=None):
        root_dir = self.root_dir
        if root_dir is None:
            return ()
        if context is None:
            context = importlib.metadata.DistributionFinder.Context()
        name = getattr(context, "name", None)
        if name is not None:
            normalized = re.sub(r"[-_.]+", "_", name).lower()
            if normalized not in MANAGED_PYTHON_TOP_LEVEL_PACKAGES:
                return ()
        scoped_context = importlib.metadata.DistributionFinder.Context(
            name=name,
            path=[str(root_dir)],
        )
        return importlib.metadata.MetadataPathFinder.find_distributions(scoped_context)


_MANAGED_PYTHON_FINDER = ManagedPackageMetaPathFinder()
_ACTIVE_MANAGED_PYTHON_ROOT: Path | None = None


def active_managed_python_root() -> Path | None:
    return _ACTIVE_MANAGED_PYTHON_ROOT


def set_managed_python_overlay(root_dir: Path | None) -> Path | None:
    """Activate or deactivate the managed pure-Python package overlay directory."""
    global _ACTIVE_MANAGED_PYTHON_ROOT
    previous = _ACTIVE_MANAGED_PYTHON_ROOT

    if root_dir is not None:
        resolved = root_dir.resolve()
        _MANAGED_PYTHON_FINDER.set_root_dir(resolved)
        if _MANAGED_PYTHON_FINDER in sys.meta_path:
            if sys.meta_path[0] is not _MANAGED_PYTHON_FINDER:
                sys.meta_path.remove(_MANAGED_PYTHON_FINDER)
                sys.meta_path.insert(0, _MANAGED_PYTHON_FINDER)
        else:
            sys.meta_path.insert(0, _MANAGED_PYTHON_FINDER)
        _ACTIVE_MANAGED_PYTHON_ROOT = resolved
    else:
        _MANAGED_PYTHON_FINDER.set_root_dir(None)
        if _MANAGED_PYTHON_FINDER in sys.meta_path:
            sys.meta_path.remove(_MANAGED_PYTHON_FINDER)
        _ACTIVE_MANAGED_PYTHON_ROOT = None

    importlib.invalidate_caches()
    return previous


def purge_yt_dlp_modules() -> None:
    prefixes = ("yt_dlp", "yt_dlp_ejs", "yt_dlp_plugins")
    for name in list(sys.modules):
        if name in prefixes or any(name.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def detect_bundled_yt_dlp_version() -> str | None:
    """Detect the bundled/environment yt-dlp version without importing yt_dlp."""
    active_str = str(_ACTIVE_MANAGED_PYTHON_ROOT) if _ACTIVE_MANAGED_PYTHON_ROOT is not None else None

    if getattr(sys, "frozen", False):
        for finder in sys.meta_path:
            if finder is _MANAGED_PYTHON_FINDER:
                continue
            get_code = getattr(finder, "get_code", None)
            if not callable(get_code):
                continue
            try:
                code = get_code("yt_dlp.version")
            except Exception:  # noqa: BLE001
                continue
            if code is not None:
                namespace: dict = {}
                try:
                    exec(code, namespace)  # noqa: S102
                except Exception:  # noqa: BLE001
                    continue
                version = namespace.get("__version__")
                if isinstance(version, str) and version.strip():
                    return version.strip()

    non_managed_paths = [entry for entry in sys.path if entry != active_str]
    try:
        context = importlib.metadata.DistributionFinder.Context(name="yt-dlp", path=non_managed_paths)
        for dist in importlib.metadata.MetadataPathFinder.find_distributions(context):
            if dist.version:
                return str(dist.version).strip()
    except Exception:  # noqa: BLE001
        pass

    for entry in non_managed_paths:
        if not entry:
            continue
        version_file = Path(entry) / "yt_dlp" / "version.py"
        if version_file.is_file():
            try:
                content = version_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", content)
            if match:
                return match.group(1).strip()

    return None


def detect_bundled_ejs_version() -> str | None:
    try:
        context = importlib.metadata.DistributionFinder.Context(name="yt-dlp-ejs", path=list(sys.path))
        for dist in importlib.metadata.MetadataPathFinder.find_distributions(context):
            if dist.version:
                return str(dist.version).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def detect_active_ejs_version() -> str | None:
    try:
        return importlib.metadata.version("yt-dlp-ejs")
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def is_valid_managed_yt_dlp_dir(directory: Path) -> bool:
    return (
        directory.is_dir()
        and (directory / "yt_dlp" / "__init__.py").is_file()
        and (directory / "yt_dlp" / "version.py").is_file()
    )


def bootstrap_managed_python_packages(data_dir: Path | None = None) -> Path | None:
    """Activate a newer managed yt-dlp overlay before yt_dlp is first imported."""
    state = load_managed_tools_state(data_dir)
    entry = state.get("yt_dlp")
    if not isinstance(entry, dict):
        return None
    raw_path = str(entry.get("path") or "").strip()
    managed_version = str(entry.get("version") or "").strip()
    managed_ejs_version = str(entry.get("ejs_version") or "").strip() or None
    if not raw_path or not managed_version:
        return None

    managed_path = Path(raw_path)
    if not is_valid_managed_yt_dlp_dir(managed_path):
        return None

    bundled_version = detect_bundled_yt_dlp_version()
    if bundled_version:
        if is_newer_version(managed_version, bundled_version):
            return None
        if version_key(managed_version) == version_key(bundled_version):
            bundled_ejs_version = detect_bundled_ejs_version()
            if not (
                managed_ejs_version
                and (bundled_ejs_version is None or is_newer_version(bundled_ejs_version, managed_ejs_version))
            ):
                return None

    set_managed_python_overlay(managed_path)
    return managed_path


def twitch_downloader_binary_name(platform_name: str | None = None) -> str:
    plat = (platform_name or sys.platform).lower()
    return "TwitchDownloaderCLI.exe" if plat.startswith("win") else "TwitchDownloaderCLI"


def twitch_downloader_asset_pattern(
    platform_name: str | None = None,
    machine: str | None = None,
) -> re.Pattern[str] | None:
    plat = (platform_name or sys.platform).lower()
    arch = (machine or platform.machine()).lower()

    if plat.startswith("win"):
        return re.compile(r"^TwitchDownloaderCLI-.*-Windows-x64\.zip$", re.IGNORECASE)
    if plat.startswith("linux"):
        if arch in {"aarch64", "arm64"}:
            return re.compile(r"^TwitchDownloaderCLI-.*-LinuxArm64\.zip$", re.IGNORECASE)
        if arch.startswith("arm"):
            return re.compile(r"^TwitchDownloaderCLI-.*-LinuxArm\.zip$", re.IGNORECASE)
        return re.compile(r"^TwitchDownloaderCLI-.*-Linux-x64\.zip$", re.IGNORECASE)
    if plat == "darwin":
        if arch in {"aarch64", "arm64"}:
            return re.compile(r"^TwitchDownloaderCLI-.*-MacOSArm64\.zip$", re.IGNORECASE)
        return re.compile(r"^TwitchDownloaderCLI-.*-MacOS-x64\.zip$", re.IGNORECASE)
    return None


def detect_twitch_downloader_version(
    executable_path: Path,
    data_dir: Path | None = None,
    state: dict | None = None,
) -> str | None:
    if not executable_path.is_file():
        return None

    # On non-Windows hosts, avoid executing a Windows .exe binary.
    if not sys.platform.startswith("win") and executable_path.suffix.lower() == ".exe":
        return None

    try:
        stat = executable_path.stat()
    except OSError:
        return None

    resolved_str = str(executable_path.resolve())
    loaded_state = state if state is not None else load_managed_tools_state(data_dir)
    probe_cache = loaded_state.get("bundled_twitch_probe")
    if (
        isinstance(probe_cache, dict)
        and probe_cache.get("path") == resolved_str
        and probe_cache.get("size") == stat.st_size
        and probe_cache.get("mtime_ns") == stat.st_mtime_ns
    ):
        cached_version = probe_cache.get("version")
        return str(cached_version).strip() if cached_version else None

    version: str | None = None
    try:
        result = subprocess.run(
            [str(executable_path), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            **subprocess_window_options(),
        )
        combined = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"TwitchDownloaderCLI\s+v?(\d+(?:\.\d+)+)", combined, flags=re.IGNORECASE)
        if match:
            version = match.group(1).strip()
    except Exception:  # noqa: BLE001
        version = None

    loaded_state["bundled_twitch_probe"] = {
        "path": resolved_str,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "version": version,
    }
    if state is None:
        try:
            save_managed_tools_state(loaded_state, data_dir)
        except OSError:
            pass
    return version


def resolve_twitch_downloader_executable(
    bundled_candidates: list[Path],
    data_dir: Path | None = None,
) -> str | None:
    bundled_path: Path | None = None
    for candidate in bundled_candidates:
        if candidate.is_file():
            # On non-Windows hosts, skip Windows .exe candidates unless nothing native exists.
            if not sys.platform.startswith("win") and candidate.suffix.lower() == ".exe":
                continue
            bundled_path = candidate
            break

    state = load_managed_tools_state(data_dir)
    entry = state.get("twitch_downloader")
    if isinstance(entry, dict):
        raw_path = str(entry.get("path") or "").strip()
        managed_version = str(entry.get("version") or "").strip()
        if raw_path and managed_version:
            managed_path = Path(raw_path)
            if managed_path.is_file():
                if bundled_path is None:
                    return str(managed_path)
                bundled_version = detect_twitch_downloader_version(bundled_path, data_dir=data_dir)
                if bundled_version is None or is_newer_version(bundled_version, managed_version):
                    return str(managed_path)

    if bundled_path is not None:
        return str(bundled_path)
    return None


def fetch_json_with_etag(
    url: str,
    headers: dict[str, str],
    etag: str | None = None,
    urlopen_kwargs: dict | None = None,
    timeout: int = 20,
) -> tuple[dict | None, str | None, bool]:
    req_headers = dict(headers)
    if etag:
        req_headers["If-None-Match"] = etag
    request = Request(url, headers=req_headers)
    try:
        with urlopen(request, timeout=timeout, **(urlopen_kwargs or {})) as response:
            payload = response.read().decode("utf-8", errors="replace")
            new_etag = response.headers.get("ETag") or etag
    except HTTPError as error:
        if error.code == 304:
            return None, etag, True
        raise

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return data, new_etag, False


def download_file_verified(
    url: str,
    destination: Path,
    headers: dict[str, str],
    expected_sha256: str | None = None,
    urlopen_kwargs: dict | None = None,
    cancel_check: Callable[[], bool] | None = None,
    timeout: int = 60,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers=headers)
    digest = hashlib.sha256()
    try:
        with urlopen(request, timeout=timeout, **(urlopen_kwargs or {})) as response:
            with destination.open("wb") as handle:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise InterruptedError("Tool update cancelled.")
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
    except Exception:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                pass
        raise

    actual_sha256 = digest.hexdigest().lower()
    if expected_sha256:
        normalized_expected = expected_sha256.strip().lower()
        if normalized_expected.startswith("sha256:"):
            normalized_expected = normalized_expected.split(":", 1)[1].strip()
        if actual_sha256 != normalized_expected:
            try:
                destination.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"SHA-256 verification failed for {destination.name} "
                f"(expected {normalized_expected[:12]}..., got {actual_sha256[:12]}...)."
            )
    return actual_sha256


def safe_extract_zip(zip_path: Path, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = destination_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            member_path = (resolved_root / member.filename).resolve()
            if member_path != resolved_root and resolved_root not in member_path.parents:
                raise RuntimeError(f"Unsafe zip member path detected: {member.filename}")
            if member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
                continue
            member_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as src, member_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def find_pure_python_wheel(urls: list[dict] | None) -> tuple[str, str | None, str]:
    if not isinstance(urls, list):
        raise RuntimeError("PyPI metadata did not list release files.")
    for item in urls:
        if not isinstance(item, dict):
            continue
        if item.get("packagetype") != "bdist_wheel":
            continue
        filename = str(item.get("filename") or "")
        url = str(item.get("url") or "")
        if filename.endswith("-none-any.whl") and url:
            digests = item.get("digests") if isinstance(item.get("digests"), dict) else {}
            sha256 = str(digests.get("sha256") or "").strip() or None
            return url, sha256, filename
    raise RuntimeError("Could not find a pure-Python wheel in PyPI metadata.")


def extract_twitch_downloader_binary(zip_path: Path, staging_dir: Path, binary_name: str) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    target_binary = staging_dir / binary_name
    binary_name_lower = binary_name.lower()
    found_member: zipfile.ZipInfo | None = None

    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            basename = Path(member.filename).name
            if basename.lower() == binary_name_lower:
                found_member = member
                break
        if found_member is None:
            raise RuntimeError(f"{binary_name} was not found inside {zip_path.name}.")

        with archive.open(found_member, "r") as src, target_binary.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    if not target_binary.is_file() or target_binary.stat().st_size == 0:
        raise RuntimeError(f"Extracted {binary_name} is empty.")
    if not sys.platform.startswith("win"):
        target_binary.chmod(0o755)
    return target_binary


def prune_unused_directories(parent_dir: Path, keep_dir: Path | None) -> None:
    if not parent_dir.is_dir():
        return
    resolved_keep = keep_dir.resolve() if keep_dir is not None else None
    for child in parent_dir.iterdir():
        try:
            if resolved_keep is not None and child.resolve() == resolved_keep:
                continue
        except OSError:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            # On Windows, an older executable or file may still be in use; skip quietly.
            continue


def cleanup_temp_dir(data_dir: Path | None = None) -> None:
    tmp_dir = managed_tools_dir(data_dir) / "tmp"
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dataclass
class ToolUpdateOutcome:
    tool: str
    status: str  # "updated", "up_to_date", "skipped", "error"
    current_version: str | None = None
    latest_version: str | None = None
    installed_path: str | None = None
    message: str = ""
    details: dict = field(default_factory=dict)


def _should_skip_by_interval(state_section: dict | None, force: bool, now: float) -> bool:
    if force or not isinstance(state_section, dict):
        return False
    last_checked = state_section.get("last_checked_at")
    if not isinstance(last_checked, (int, float)):
        return False
    return (now - float(last_checked)) < TOOL_UPDATE_CHECK_INTERVAL_SECONDS


def update_yt_dlp_if_needed(
    current_version: str,
    current_ejs_version: str | None,
    user_agent: str,
    *,
    force: bool = False,
    data_dir: Path | None = None,
    urlopen_kwargs: dict | None = None,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ToolUpdateOutcome:
    state = load_managed_tools_state(data_dir)
    section = state.get("yt_dlp") if isinstance(state.get("yt_dlp"), dict) else {}
    now = time.time()

    if _should_skip_by_interval(section, force, now):
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="skipped",
            current_version=current_version,
            latest_version=str(section.get("latest_seen_version") or current_version),
        )

    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    use_etag: str | None = None
    if (
        not force
        and section.get("etag")
        and section.get("latest_seen_version")
        and not is_newer_version(current_version, str(section.get("latest_seen_version")))
    ):
        use_etag = str(section["etag"])

    try:
        data, new_etag, not_modified = fetch_json_with_etag(
            PYPI_YT_DLP_URL,
            headers=headers,
            etag=use_etag,
            urlopen_kwargs=urlopen_kwargs,
        )
    except (HTTPError, URLError, OSError, json.JSONDecodeError, RuntimeError) as error:
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="error",
            current_version=current_version,
            message=f"Could not check yt-dlp updates: {error}",
        )

    if not_modified or data is None:
        section["last_checked_at"] = now
        if new_etag:
            section["etag"] = new_etag
        state["yt_dlp"] = section
        save_managed_tools_state(state, data_dir)
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="up_to_date",
            current_version=current_version,
            latest_version=str(section.get("latest_seen_version") or current_version),
        )

    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    latest_version = str(info.get("version") or "").strip()
    if not latest_version:
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="error",
            current_version=current_version,
            message="PyPI metadata for yt-dlp did not include a version.",
        )

    requires_python = info.get("requires_python")
    if not python_satisfies_requires_python(requires_python):
        section["last_checked_at"] = now
        section["latest_seen_version"] = latest_version
        if new_etag:
            section["etag"] = new_etag
        state["yt_dlp"] = section
        save_managed_tools_state(state, data_dir)
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="skipped",
            current_version=current_version,
            latest_version=latest_version,
            message=(
                f"Skipped yt-dlp {latest_version} because it requires Python {requires_python} "
                f"(current runtime is {sys.version_info.major}.{sys.version_info.minor})."
            ),
        )

    required_ejs_version = parse_required_ejs_version(info.get("requires_dist"))
    needs_yt_dlp = is_newer_version(current_version, latest_version)
    needs_ejs = bool(
        required_ejs_version
        and (
            current_ejs_version is None
            or is_newer_version(current_ejs_version, required_ejs_version)
        )
        and not is_newer_version(latest_version, current_version)
    )

    if not needs_yt_dlp and not needs_ejs:
        section["last_checked_at"] = now
        section["latest_seen_version"] = latest_version
        if new_etag:
            section["etag"] = new_etag
        state["yt_dlp"] = section
        save_managed_tools_state(state, data_dir)
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="up_to_date",
            current_version=current_version,
            latest_version=latest_version,
        )

    if log is not None:
        log(f"Downloading yt-dlp {latest_version} update...")

    python_root = managed_tools_dir(data_dir) / "python"
    tmp_dir = managed_tools_dir(data_dir) / "tmp" / f"yt-dlp-{os.getpid()}-{time.time_ns()}"
    staging_dir = python_root / f".staging-yt-dlp-{latest_version}-{os.getpid()}-{time.time_ns()}"
    try:
        yt_dlp_wheel_url, yt_dlp_sha256, yt_dlp_wheel_name = find_pure_python_wheel(data.get("urls"))
        yt_dlp_wheel_path = tmp_dir / yt_dlp_wheel_name
        download_file_verified(
            yt_dlp_wheel_url,
            yt_dlp_wheel_path,
            headers={"User-Agent": user_agent},
            expected_sha256=yt_dlp_sha256,
            urlopen_kwargs=urlopen_kwargs,
            cancel_check=cancel_check,
        )

        ejs_wheel_path: Path | None = None
        installed_ejs_version: str | None = required_ejs_version
        ejs_url = (
            PYPI_YT_DLP_EJS_URL_TEMPLATE.format(version=required_ejs_version)
            if required_ejs_version
            else PYPI_YT_DLP_EJS_LATEST_URL
        )
        try:
            ejs_data, _, _ = fetch_json_with_etag(
                ejs_url,
                headers=headers,
                urlopen_kwargs=urlopen_kwargs,
            )
            if isinstance(ejs_data, dict):
                ejs_info = ejs_data.get("info") if isinstance(ejs_data.get("info"), dict) else {}
                installed_ejs_version = str(ejs_info.get("version") or installed_ejs_version or "").strip() or None
                ejs_wheel_url, ejs_sha256, ejs_wheel_name = find_pure_python_wheel(ejs_data.get("urls"))
                ejs_wheel_path = tmp_dir / ejs_wheel_name
                download_file_verified(
                    ejs_wheel_url,
                    ejs_wheel_path,
                    headers={"User-Agent": user_agent},
                    expected_sha256=ejs_sha256,
                    urlopen_kwargs=urlopen_kwargs,
                    cancel_check=cancel_check,
                )
        except InterruptedError:
            raise
        except Exception as ejs_error:  # noqa: BLE001
            if log is not None:
                log(f"Could not download yt-dlp-ejs companion wheel (using bundled/remote fallback): {ejs_error}")

        safe_extract_zip(yt_dlp_wheel_path, staging_dir)
        if ejs_wheel_path is not None and ejs_wheel_path.is_file():
            safe_extract_zip(ejs_wheel_path, staging_dir)

        if not is_valid_managed_yt_dlp_dir(staging_dir):
            raise RuntimeError("Extracted yt-dlp wheel is missing expected package files.")

        target_dir = python_root / f"yt-dlp-{latest_version}"
        if target_dir.exists():
            target_dir = python_root / f"yt-dlp-{latest_version}-{int(time.time())}"
        staging_dir.rename(target_dir)

        section.update({
            "version": latest_version,
            "ejs_version": installed_ejs_version,
            "path": str(target_dir),
            "updated_at": now,
            "last_checked_at": now,
            "latest_seen_version": latest_version,
        })
        if new_etag:
            section["etag"] = new_etag
        state["yt_dlp"] = section
        save_managed_tools_state(state, data_dir)

        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="updated",
            current_version=current_version,
            latest_version=latest_version,
            installed_path=str(target_dir),
            details={"ejs_version": installed_ejs_version},
        )
    except InterruptedError:
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="skipped",
            current_version=current_version,
            message="yt-dlp update cancelled.",
        )
    except Exception as error:  # noqa: BLE001
        return ToolUpdateOutcome(
            tool="yt-dlp",
            status="error",
            current_version=current_version,
            latest_version=latest_version,
            message=f"Could not update yt-dlp to {latest_version}: {error}",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)


def update_twitch_downloader_if_needed(
    bundled_candidates: list[Path],
    user_agent: str,
    *,
    force: bool = False,
    data_dir: Path | None = None,
    urlopen_kwargs: dict | None = None,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ToolUpdateOutcome:
    asset_pattern = twitch_downloader_asset_pattern()
    if asset_pattern is None:
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="skipped",
            message="No TwitchDownloaderCLI release asset matches this platform.",
        )

    state = load_managed_tools_state(data_dir)
    section = state.get("twitch_downloader") if isinstance(state.get("twitch_downloader"), dict) else {}
    now = time.time()

    bundled_path: Path | None = None
    for candidate in bundled_candidates:
        if candidate.is_file():
            if not sys.platform.startswith("win") and candidate.suffix.lower() == ".exe":
                continue
            bundled_path = candidate
            break

    managed_path = Path(str(section.get("path") or "")) if section.get("path") else None
    managed_version = str(section.get("version") or "").strip() or None
    bundled_version = detect_twitch_downloader_version(bundled_path, data_dir=data_dir, state=state) if bundled_path else None

    current_version: str | None = None
    if managed_path is not None and managed_path.is_file() and managed_version:
        if bundled_version is None or is_newer_version(bundled_version, managed_version):
            current_version = managed_version
        else:
            current_version = bundled_version
    elif bundled_version:
        current_version = bundled_version

    if _should_skip_by_interval(section, force, now) and current_version is not None:
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="skipped",
            current_version=current_version,
            latest_version=str(section.get("latest_seen_version") or current_version),
        )

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
    }
    use_etag: str | None = None
    if (
        not force
        and current_version is not None
        and section.get("etag")
        and section.get("latest_seen_version")
        and not is_newer_version(current_version, str(section.get("latest_seen_version")))
    ):
        use_etag = str(section["etag"])

    try:
        data, new_etag, not_modified = fetch_json_with_etag(
            TWITCH_DOWNLOADER_LATEST_RELEASE_URL,
            headers=headers,
            etag=use_etag,
            urlopen_kwargs=urlopen_kwargs,
        )
    except (HTTPError, URLError, OSError, json.JSONDecodeError, RuntimeError) as error:
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="error",
            current_version=current_version,
            message=f"Could not check TwitchDownloaderCLI updates: {error}",
        )

    if not_modified or data is None:
        section["last_checked_at"] = now
        if new_etag:
            section["etag"] = new_etag
        state["twitch_downloader"] = section
        save_managed_tools_state(state, data_dir)
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="up_to_date",
            current_version=current_version,
            latest_version=str(section.get("latest_seen_version") or current_version),
        )

    latest_version = str(data.get("tag_name") or "").lstrip("vV").strip()
    if not latest_version:
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="error",
            current_version=current_version,
            message="GitHub release for TwitchDownloaderCLI did not include a tag_name.",
        )

    if current_version and not is_newer_version(current_version, latest_version):
        section["last_checked_at"] = now
        section["latest_seen_version"] = latest_version
        if new_etag:
            section["etag"] = new_etag
        state["twitch_downloader"] = section
        save_managed_tools_state(state, data_dir)
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="up_to_date",
            current_version=current_version,
            latest_version=latest_version,
        )

    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    matched_asset: dict | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_name = str(asset.get("name") or "")
        if asset_pattern.match(asset_name) and asset.get("browser_download_url"):
            matched_asset = asset
            break

    if matched_asset is None:
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="error",
            current_version=current_version,
            latest_version=latest_version,
            message=f"No compatible TwitchDownloaderCLI archive found for release {latest_version}.",
        )

    if log is not None:
        log(f"Downloading TwitchDownloaderCLI {latest_version} update...")

    binary_name = twitch_downloader_binary_name()
    td_root = managed_tools_dir(data_dir) / "twitch-downloader"
    tmp_dir = managed_tools_dir(data_dir) / "tmp" / f"twitch-{os.getpid()}-{time.time_ns()}"
    staging_dir = td_root / f".staging-{latest_version}-{os.getpid()}-{time.time_ns()}"
    try:
        asset_url = str(matched_asset["browser_download_url"])
        asset_name = str(matched_asset["name"])
        asset_digest = str(matched_asset.get("digest") or "").strip() or None
        zip_path = tmp_dir / asset_name
        download_file_verified(
            asset_url,
            zip_path,
            headers={"User-Agent": user_agent},
            expected_sha256=asset_digest,
            urlopen_kwargs=urlopen_kwargs,
            cancel_check=cancel_check,
            timeout=120,
        )

        extract_twitch_downloader_binary(zip_path, staging_dir, binary_name)
        target_dir = td_root / latest_version
        if target_dir.exists():
            target_dir = td_root / f"{latest_version}-{int(time.time())}"
        staging_dir.rename(target_dir)
        installed_binary = target_dir / binary_name

        section.update({
            "version": latest_version,
            "path": str(installed_binary),
            "updated_at": now,
            "last_checked_at": now,
            "latest_seen_version": latest_version,
        })
        if new_etag:
            section["etag"] = new_etag
        state["twitch_downloader"] = section
        save_managed_tools_state(state, data_dir)
        prune_unused_directories(td_root, target_dir)

        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="updated",
            current_version=current_version,
            latest_version=latest_version,
            installed_path=str(installed_binary),
        )
    except InterruptedError:
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="skipped",
            current_version=current_version,
            message="TwitchDownloaderCLI update cancelled.",
        )
    except Exception as error:  # noqa: BLE001
        return ToolUpdateOutcome(
            tool="TwitchDownloaderCLI",
            status="error",
            current_version=current_version,
            latest_version=latest_version,
            message=f"Could not update TwitchDownloaderCLI to {latest_version}: {error}",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)


def update_all_tools(
    current_yt_dlp_version: str,
    current_ejs_version: str | None,
    twitch_bundled_candidates: list[Path],
    user_agent: str,
    *,
    force: bool = False,
    data_dir: Path | None = None,
    urlopen_kwargs: dict | None = None,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[ToolUpdateOutcome]:
    cleanup_temp_dir(data_dir)
    outcomes = [
        update_yt_dlp_if_needed(
            current_version=current_yt_dlp_version,
            current_ejs_version=current_ejs_version,
            user_agent=user_agent,
            force=force,
            data_dir=data_dir,
            urlopen_kwargs=urlopen_kwargs,
            log=log,
            cancel_check=cancel_check,
        ),
        update_twitch_downloader_if_needed(
            bundled_candidates=twitch_bundled_candidates,
            user_agent=user_agent,
            force=force,
            data_dir=data_dir,
            urlopen_kwargs=urlopen_kwargs,
            log=log,
            cancel_check=cancel_check,
        ),
    ]
    return outcomes
