"""Установщик Takt: GUI (tkinter) + тихий режим /S для автообновлений.

Режимы:
  Takt-Setup.exe            — GUI-установка
  Takt-Setup.exe /S         — тихая установка (используется механизмом обновлений)
  Takt-Setup.exe /S /DIR=X  — тихая установка в каталог X
  uninstall.exe /uninstall [/S] — удаление
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

APP_NAME = "Takt"
VERSION = "0.3.0"
EXE_NAME = "Takt.exe"
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Takt"


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_zip() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "app.zip"


def default_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_NAME


def is_app_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}"],
                         capture_output=True, text=True).stdout
    return EXE_NAME.lower() in out.lower()


def wait_app_close(timeout: float = 20.0) -> None:
    t0 = time.time()
    while is_app_running() and time.time() - t0 < timeout:
        time.sleep(1)


def extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for attempt in range(15):
            try:
                z.extractall(dest)
                return
            except PermissionError:
                time.sleep(1)
        z.extractall(dest)


def make_shortcut(target: Path, lnk: Path) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(lnk) + "');"
          "$s.TargetPath='" + str(target) + "';"
          "$s.WorkingDirectory='" + str(target.parent) + "';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


def shortcut_paths():
    start = (Path(os.environ.get("APPDATA", str(Path.home())))
             / "Microsoft/Windows/Start Menu/Programs" / f"{APP_NAME}.lnk")
    desktop = Path.home() / "Desktop" / f"{APP_NAME}.lnk"
    return start, desktop


def register_uninstall(dest: Path) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(dest))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(dest / EXE_NAME))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          f'"{dest / "uninstall.exe"}" /uninstall')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def unregister_uninstall() -> None:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINST_KEY)
    except OSError:
        pass


def installed_dir() -> Path:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as key:
            return Path(winreg.QueryValueEx(key, "InstallLocation")[0])
    except OSError:
        return default_dir()


def install(dest: Path, desktop: bool = True, launch: bool = True,
            progress=None) -> None:
    def say(msg):
        if progress:
            progress(msg)

    say("Ожидание закрытия приложения…")
    wait_app_close()
    say("Распаковка файлов…")
    extract(resource_zip(), dest)
    exe = dest / EXE_NAME
    say("Создание ярлыков…")
    start, desk = shortcut_paths()
    make_shortcut(exe, start)
    if desktop:
        make_shortcut(exe, desk)
    if frozen():
        shutil.copy2(sys.executable, dest / "uninstall.exe")
    register_uninstall(dest)
    say("Готово")
    if launch:
        subprocess.Popen([str(exe)], cwd=str(dest))


def uninstall(progress=None) -> None:
    def say(msg):
        if progress:
            progress(msg)

    dest = installed_dir()
    say("Ожидание закрытия приложения…")
    wait_app_close()
    start, desk = shortcut_paths()
    for lnk in (start, desk):
        try:
            lnk.unlink()
        except OSError:
            pass
    say("Удаление файлов…")
    for attempt in range(10):
        try:
            shutil.rmtree(dest)
            break
        except PermissionError:
            time.sleep(1)
    unregister_uninstall()
    if dest.exists():
        # работающий exe не может удалить себя — отложенная очистка после выхода
        subprocess.Popen(
            f'cmd.exe /c ping -n 3 127.0.0.1 >nul'
            f' & del /f /q "{dest / "uninstall.exe"}" & rmdir "{dest}"',
            creationflags=0x08000000 | 0x00000008, close_fds=True)
    say("Удалено")


def run_gui() -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"Установка {APP_NAME} {VERSION}")
    root.geometry("460x260")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=f"{APP_NAME} {VERSION} — менеджер задач",
              font=("Segoe UI", 11, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Каталог установки:").pack(anchor="w", pady=(12, 2))
    dir_var = tk.StringVar(value=str(default_dir()))
    ttk.Entry(frm, textvariable=dir_var).pack(fill="x")
    desktop_var = tk.BooleanVar(value=True)
    launch_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="Ярлык на рабочем столе",
                    variable=desktop_var).pack(anchor="w", pady=(8, 0))
    ttk.Checkbutton(frm, text="Запустить после установки",
                    variable=launch_var).pack(anchor="w")
    status = ttk.Label(frm, text="")
    status.pack(anchor="w", pady=(8, 0))
    btn = ttk.Button(frm, text="Установить")
    btn.pack(anchor="e", pady=(8, 0))

    def do_install():
        btn.state(["disabled"])

        def progress(msg):
            status.config(text=msg)
            root.update_idletasks()

        def worker():
            try:
                install(Path(dir_var.get()), desktop_var.get(), launch_var.get(),
                        progress)
                status.config(text="Установка завершена")
                root.after(1200, root.destroy)
            except Exception as ex:
                status.config(text=f"Ошибка: {ex}")
                btn.state(["!disabled"])

        import threading
        threading.Thread(target=worker, daemon=True).start()

    btn.config(command=do_install)
    root.mainloop()


def main() -> None:
    args = sys.argv[1:]
    silent = "/S" in args or "/s" in args
    dir_arg = next((a[5:] for a in args if a.startswith("/DIR=")), None)
    if "/uninstall" in args:
        if silent:
            uninstall()
        else:
            import tkinter as tk
            from tkinter import messagebox, ttk
            root = tk.Tk()
            root.withdraw()
            if messagebox.askyesno(f"Удаление {APP_NAME}",
                                   f"Удалить {APP_NAME} с этого компьютера?"):
                uninstall()
                messagebox.showinfo(APP_NAME, "Приложение удалено")
            root.destroy()
        return
    dest = Path(dir_arg) if dir_arg else default_dir()
    if silent:
        install(dest, desktop=True, launch=False)
    else:
        run_gui()


if __name__ == "__main__":
    main()