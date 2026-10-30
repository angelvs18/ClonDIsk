"""Las ventanas caben en una pantalla de 1366x768 (área útil ~1366x728)."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 no está disponible")

from PySide6.QtCore import QPoint, QRect  # noqa: E402

from clondisk.core.planner import CloneOptions, build_plan  # noqa: E402
from clondisk.platforms.base import Platform  # noqa: E402

from test_core import real_source, real_target  # noqa: E402

SCREEN_W, SCREEN_H = 1366, 728
MAX_MIN_HEIGHT = 680


@pytest.fixture(scope="module")
def app():
    from clondisk.gui import theme
    inst = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    theme.apply(inst)   # el tema cambia fuentes y márgenes: medir con él aplicado
    return inst


def _inside(dialog, widget) -> bool:
    top_left = widget.mapTo(dialog, QPoint(0, 0))
    return dialog.rect().contains(QRect(top_left, widget.size()))


def test_wizard_fits_1366x768(app):
    from clondisk.gui.wizard import CloneWizard
    src, tgt = real_source(), real_target()
    w = CloneWizard(Platform(), [src, tgt], src)
    w.target = tgt
    w.resize(SCREEN_W, SCREEN_H)
    w.show()
    try:
        for i in range(w.stack.count()):
            w.show_page(i)
            app.processEvents()
            hint = w.minimumSizeHint()
            assert hint.height() <= MAX_MIN_HEIGHT, (i, hint.toTuple())
            assert hint.width() <= SCREEN_W, (i, hint.toTuple())
            assert w.minimumWidth() <= SCREEN_W and w.minimumHeight() <= SCREEN_H
            for b in (w.btn_back, w.btn_next, w.btn_cancel):
                assert b.isVisible() and _inside(w, b), (i, b.text())
    finally:
        w.close()


def test_progress_dialog_fits_with_many_boot_checks(app):
    from clondisk.core.engine import CloneResult
    from clondisk.gui.progress import ProgressDialog
    d = ProgressDialog(Platform(), build_plan(real_source(), real_target(), CloneOptions()))
    checks = [(i % 2 == 0, "comprobación de arranque " * 4) for i in range(40)]
    d._on_finished(CloneResult(True, "ok", boot_checks=checks, bad_sectors=list(range(0, 9000, 3))))
    d.resize(SCREEN_W, SCREEN_H)
    d.show()
    app.processEvents()
    try:
        assert d.minimumSizeHint().height() <= MAX_MIN_HEIGHT
        assert d.minimumSizeHint().width() <= SCREEN_W
        assert _inside(d, d.btn_close) and _inside(d, d.btn_save)
    finally:
        d.close()


def test_main_window_min_width_fits(app):
    from clondisk.gui.main_window import MainWindow
    m = MainWindow(Platform())          # sin refresh(): no lista discos
    try:
        assert m.minimumSizeHint().width() <= SCREEN_W
        assert m.minimumSizeHint().height() <= MAX_MIN_HEIGHT
    finally:
        m.close()
