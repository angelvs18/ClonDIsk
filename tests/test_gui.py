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


# ---------------------------------------------------------------- «E:» una sola vez
def test_drive_letter_shown_once(app):
    from clondisk.core.winprep import DriverBackup
    from clondisk.gui.wizard import (CloneWizard, driver_backup_done_text, driver_backup_error_text,
                                     windows_label)
    src, tgt = real_source(), real_target()
    win = src.partitions[1]
    assert win.display_name() == "E:"
    err = FileNotFoundError(2, "El sistema no puede encontrar el archivo especificado")
    texts = [windows_label(win), driver_backup_error_text(win, err),
             driver_backup_done_text(win, DriverBackup(r"C:\ClonDisk\drivers_x", 12, "dism",
                                                       used_fallback_dir=True))]
    w = CloneWizard(Platform(), [src, tgt], src)
    w.target = tgt
    try:
        w.show_page(2)
        texts.append(w.lbl_win.text())
        w._read_options()
        w.show_page(3)
        w.show_page(4)
        texts.append(w.summary.toPlainText())
    finally:
        w.close()
    for t in texts:
        assert "E::" not in t, t
    assert texts[0].count("E:") == 1 and texts[1].count("E:") == 1
    assert "Windows 10 Pro" in texts[3] and "C:\\ClonDisk" in texts[2]


# ---------------------------------------------------------------- Acceso controlado a carpetas
def _wait_idle(app, w, timeout=10.0):
    import time
    end = time.monotonic() + timeout
    while (w._busy or w._preflight_running) and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert not w._busy


def test_summary_blocks_start_until_cfa_resolved(app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from clondisk.core.winprep import CfaStatus
    from clondisk.gui.wizard import CloneWizard
    src, tgt = real_source(), real_target()
    tgt.size_bytes = 1000 * 1024 ** 3                  # el origen cabe sin reducir
    w = CloneWizard(Platform(), [src, tgt], src)
    w.target = tgt
    try:
        w.show_page(2)
        w._read_options()
        w.show_page(3)
        w.show_page(4)
        _wait_idle(app, w)                              # consulta de salud en segundo plano
        assert w.plan.ok, w.plan.errors
        w.confirm.setChecked(True)
        assert w.btn_next.isEnabled()                   # sin bloqueo: se puede iniciar
        w.cfa, w.cfa_checked = CfaStatus(1, [], r"C:\Apps\ClonDisk.exe"), True
        w._update_cfa_box()
        w.confirm.setChecked(True)
        assert not w.cfa_box.isHidden() and "ClonDisk.exe" in w.cfa_label.text()
        assert not w.btn_next.isEnabled()
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
        w._cfa_skip()                                   # confirmación explícita
        assert w.cfa_override and w.btn_next.isEnabled()
        w.cfa_override = False
        w.cfa = CfaStatus(1, [r"c:\apps\clondisk.exe"], r"C:\Apps\ClonDisk.exe")
        w._update_cfa_box()
        assert w.cfa_box.isHidden() and w.btn_next.isEnabled()
    finally:
        w.close()
