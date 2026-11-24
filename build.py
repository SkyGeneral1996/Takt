"""Сборка дистрибутивов Takt под Windows.

Результат в dist/:
  Takt-Portable.exe  — standalone, без установки
  Takt/              — one-folder бандл
  Takt-Setup.exe     — установщик (GUI + /S для автообновлений)

Важно: flet pack -y стирает build/ и dist/ перед каждой сборкой,
поэтому готовые артефакты между шагами хранятся в release/.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
FLET = ROOT / ".venv" / "Scripts" / "flet.exe"
DIST = ROOT / "dist"
STAGE = ROOT / "release"
ICON = ROOT / "assets" / "icon.ico"

sys.path.insert(0, str(ROOT))
from takt import __version__ as VERSION


def write_native_stamp():
    """Отпечаток нативной оболочки: при его смене Android-обновление
    требует новый APK, иначе доставляется только код (Takt-Code.zip)."""
    import flet
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject.get("project", {}).pop("version", None)
    h = hashlib.sha256()
    h.update(flet.version.flet_version.encode())
    h.update((ROOT / "requirements.txt").read_bytes())
    h.update(json.dumps(pyproject, sort_keys=True).encode())
    for name in ("icon.png", "icon.ico"):
        h.update((ROOT / "assets" / name).read_bytes())
    (ROOT / "takt" / "native_stamp.json").write_text(
        json.dumps({"native": h.hexdigest(), "version": VERSION}),
        encoding="utf-8")
    print("native stamp:", h.hexdigest()[:12], flush=True)


def run(*args):
    print(">>", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT)


def main():
    STAGE.mkdir(exist_ok=True)
    write_native_stamp()

    run(FLET, "pack", "main.py", "-n", "Takt", "-D", "--distpath", DIST,
        "--product-name", "Takt", "--product-version", VERSION, "--icon", str(ICON), "-y")

    app_zip = STAGE / "app.zip"
    with zipfile.ZipFile(app_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (DIST / "Takt").rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(DIST / "Takt"))
    print("app.zip:", app_zip.stat().st_size, "bytes", flush=True)
    shutil.move(str(DIST / "Takt"), str(STAGE / "Takt"))

    run(FLET, "pack", "installer.py", "-n", "Takt-Setup", "--distpath", DIST,
        "--add-data", f"{app_zip};.",
        "--product-name", "Takt Setup", "--product-version", VERSION, "--icon", str(ICON), "-y",
        "--pyinstaller-build-args=--noconsole")
    shutil.move(str(DIST / "Takt-Setup.exe"), str(STAGE / "Takt-Setup.exe"))

    run(FLET, "pack", "main.py", "-n", "Takt-Portable", "--distpath", DIST,
        "--product-name", "Takt", "--product-version", VERSION, "--icon", str(ICON), "-y")
    shutil.move(str(DIST / "Takt-Portable.exe"), str(STAGE / "Takt-Portable.exe"))

    shutil.rmtree(DIST, ignore_errors=True)
    DIST.mkdir()
    for name in ("Takt", "Takt-Setup.exe", "Takt-Portable.exe"):
        shutil.move(str(STAGE / name), str(DIST / name))
    app_zip.unlink()
    STAGE.rmdir()

    print("\nГотово:", flush=True)
    for p in sorted(DIST.iterdir()):
        print(" ", p.name, flush=True)


if __name__ == "__main__":
    main()