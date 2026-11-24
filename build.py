"""Сборка дистрибутивов Takt под Windows.

Результат в dist/:
  Takt-Portable.exe  — standalone, без установки
  Takt/              — one-folder бандл
  Takt-Setup.exe     — установщик (GUI + /S для автообновлений)

Важно: flet pack -y стирает build/ и dist/ перед каждой сборкой,
поэтому готовые артефакты между шагами хранятся в release/.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
FLET = ROOT / ".venv" / "Scripts" / "flet.exe"
DIST = ROOT / "dist"
STAGE = ROOT / "release"


def run(*args):
    print(">>", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=ROOT)


def main():
    STAGE.mkdir(exist_ok=True)

    run(FLET, "pack", "main.py", "-n", "Takt", "-D", "--distpath", DIST,
        "--product-name", "Takt", "--product-version", "0.2.0", "-y")

    app_zip = STAGE / "app.zip"
    with zipfile.ZipFile(app_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (DIST / "Takt").rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(DIST / "Takt"))
    print("app.zip:", app_zip.stat().st_size, "bytes", flush=True)
    shutil.move(str(DIST / "Takt"), str(STAGE / "Takt"))

    run(FLET, "pack", "installer.py", "-n", "Takt-Setup", "--distpath", DIST,
        "--add-data", f"{app_zip};.",
        "--product-name", "Takt Setup", "--product-version", "0.2.0", "-y",
        "--pyinstaller-build-args=--noconsole")
    shutil.move(str(DIST / "Takt-Setup.exe"), str(STAGE / "Takt-Setup.exe"))

    run(FLET, "pack", "main.py", "-n", "Takt-Portable", "--distpath", DIST,
        "--product-name", "Takt", "--product-version", "0.2.0", "-y")
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