import sys

import flet as ft

from tasksync.ui.app import main

if __name__ == "__main__":
    if "--web" in sys.argv:
        ft.run(main, view=ft.AppView.WEB_BROWSER, port=8550)
    else:
        ft.run(main)