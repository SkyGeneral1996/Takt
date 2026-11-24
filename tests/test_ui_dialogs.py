import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from takt.core.models import Task
from takt.core.store import Store
from takt.update import UpdateInfo


class FakePage:
    def __init__(self):
        self.title = ""
        self.theme = None
        self.theme_mode = None
        self.padding = 0
        self.appbar = None
        self.floating_action_button = None
        self.services = []
        self.dialogs = []
        self.window = SimpleNamespace(icon=None)

    def add(self, *c):
        pass

    def update(self):
        pass

    def show_dialog(self, d):
        self.dialogs.append(d)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()


class TestDialogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "d.db", device_id="devD")
        self.store.save_task(Task(title="Задача"))
        from takt.ui.app import TaktApp
        self.app = TaktApp(FakePage(), self.store)
        self.app.scheduler.stop()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_settings_builds(self):
        from takt.ui.settings import open_settings
        open_settings(self.app)
        self.assertEqual(len(self.app.page.dialogs), 1)

    def test_send_builds_qr(self):
        from takt.ui.settings import open_send
        open_send(self.app)
        dlg = self.app.page.dialogs[-1]
        images = [c for c in dlg.content.content.controls
                  if getattr(c, "src", None)]
        self.assertTrue(images)

    def test_receive_and_diff(self):
        from takt.ui.settings import open_receive, _show_diff
        from takt.transfer import build_payload
        open_receive(self.app)
        payload = build_payload(self.store)
        _show_diff(self.app, payload)
        self.assertEqual(len(self.app.page.dialogs), 2)

    def test_update_dialog_builds(self):
        from takt.ui.settings import _show_update_dialog
        info = UpdateInfo(version="9.9.9", tag="v9.9.9", notes="test",
                          assets=[{"name": "Takt-Setup.exe", "url": "u", "size": 1}],
                          url="u")
        _show_update_dialog(self.app, info)
        self.assertEqual(len(self.app.page.dialogs), 1)


if __name__ == "__main__":
    unittest.main()