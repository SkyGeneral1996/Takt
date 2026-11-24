from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__

API = "https://api.github.com"


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    notes: str
    assets: list[dict]
    url: str


def parse_version(text: str) -> tuple:
    text = text.strip().lstrip("vV")
    parts = []
    for chunk in text.split(".")[:4]:
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def current_version() -> str:
    return __version__


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def check_update(repo: str, current: str | None = None) -> UpdateInfo | None:
    """repo в формате owner/name. None, если обновлений нет."""
    repo = repo.strip().strip("/")
    if repo.count("/") != 1:
        raise UpdateError("Репозиторий должен быть в формате owner/name")
    resp = requests.get(
        f"{API}/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json"}, timeout=15)
    if resp.status_code == 404:
        raise UpdateError("Репозиторий или релизы не найдены. "
                          "Создайте релиз на GitHub после публикации репозитория.")
    if resp.status_code != 200:
        raise UpdateError(f"GitHub ответил HTTP {resp.status_code}")
    data = resp.json()
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(current or current_version()):
        return None
    return UpdateInfo(
        version=tag.lstrip("vV"), tag=tag,
        notes=(data.get("body") or "")[:2000],
        assets=[{"name": a["name"], "url": a["browser_download_url"],
                 "size": a.get("size", 0)} for a in data.get("assets", [])],
        url=data.get("html_url", ""))


def choose_asset(assets: list[dict]) -> dict | None:
    exe = [a for a in assets if a["name"].lower().endswith(".exe")]
    for a in exe:
        if "setup" in a["name"].lower():
            return a
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    if zips:
        return zips[0]
    return exe[0] if exe else None


def download_asset(url: str, dest: Path, progress=None) -> Path:
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    return dest


def launch_update(asset_path: Path) -> None:
    """Установщик запускаем и выходим; portable/zip — запускаем новую копию."""
    path = Path(asset_path)
    if path.suffix.lower() == ".zip":
        target = Path(tempfile.mkdtemp(prefix="takt-update-"))
        with zipfile.ZipFile(path) as z:
            z.extractall(target)
        exes = list(target.rglob("*.exe"))
        if not exes:
            raise UpdateError("В архиве обновления не найден .exe")
        path = exes[0]
    subprocess.Popen([str(path)], close_fds=True)