"""Ventana de progreso y ejecución del motor en un hilo aparte."""
from __future__ import annotations

import html
import os
import threading
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QTextEdit, QVBoxLayout, QWidget)

from ..core.engine import STEPS, CloneEngine, CloneResult, fmt_lbas
from ..core.model import fmt_size
from ..core.planner import ClonePlan
from . import theme


def _keep_awake(on: bool):
    """Evita que Windows suspenda el equipo durante la clonación."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


class CloneWorker(QObject):
    log = Signal(str, str)
    step = Signal(int, str)
    progress = Signal(object, object, float)
    finished = Signal(object)

    def __init__(self, platform, plan: ClonePlan):
        super().__init__()
        self.platform = platform
        self.plan = plan
        self.cancel = threading.Event()

    def run(self):
        _keep_awake(True)
        try:
            engine = CloneEngine(self.platform, self.plan,
                                 log=lambda m, l="info": self.log.emit(m, l),
                                 step=lambda i, n: self.step.emit(i, n),
                                 progress=lambda d, t, s: self.progress.emit(d, t, s),
                                 cancel=self.cancel)
            result = engine.run()
        except Exception as e:  # salvaguarda
            result = CloneResult(False, f"Error inesperado: {e}")
        finally:
            _keep_awake(False)
        self.finished.emit(result)


class TaskWorker(QObject):
    """Ejecuta una función cualquiera fuera del hilo de la interfaz."""
    done = Signal(object, object)  # (resultado, excepción)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        try:
            self.done.emit(self.fn(*self.args), None)
        except Exception as e:
            self.done.emit(None, e)


class _Relay(QObject):
    """Vive en el hilo de la interfaz para recibir el resultado del trabajo."""

    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback

    @Slot(object, object)
    def handle(self, res, err):
        self.callback(res, err)


def run_in_thread(parent, fn, *args, on_done=None):
    """Lanza ``fn`` en un QThread y llama ``on_done(resultado, error)`` en la GUI."""
    th = QThread(parent)
    w = TaskWorker(fn, *args)
    w.moveToThread(th)
    th.started.connect(w.run)

    def finish(res, err):
        th.quit()
        th.wait()
        try:
            if on_done:
                on_done(res, err)
        finally:
            w.deleteLater()
            th.deleteLater()
            relay.deleteLater()

    relay = _Relay(parent, finish)
    w.done.connect(relay.handle, Qt.QueuedConnection)
    th.start()
    return th


def fmt_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


class ProgressDialog(QDialog):
    def __init__(self, platform, plan: ClonePlan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clonando disco")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint
                            | Qt.WindowMinMaxButtonsHint)
        theme.fit_to_screen(self, 900, 640)
        self.setModal(True)
        self.result: CloneResult | None = None
        self._running = False
        self._t0 = time.monotonic()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.steps = QListWidget()
        self.steps.setObjectName("StepList")
        self.steps.setFixedWidth(290)
        self.steps.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.steps.setFocusPolicy(Qt.NoFocus)
        for name in STEPS:
            QListWidgetItem(f"○  {name}", self.steps)
        root.addWidget(self.steps)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        title = QLabel(f"Disco {plan.source.number} → Disco {plan.target.number}")
        title.setObjectName("PageTitle")
        lay.addWidget(title)
        sub = QLabel(f"{plan.source.model}  →  {plan.target.model}")
        sub.setObjectName("PageSubtitle")
        lay.addWidget(sub)

        self.banner = QLabel("")
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        lay.addWidget(self.banner)
        self.checks = QLabel("")
        self.checks.setObjectName("Card")
        self.checks.setWordWrap(True)
        self.checks.setTextFormat(Qt.RichText)
        self.checks.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Con scroll y altura limitada: una lista larga no empuja los botones fuera de la pantalla.
        self.checks_area = QScrollArea()
        self.checks_area.setWidgetResizable(True)
        self.checks_area.setFrameShape(QFrame.NoFrame)
        self.checks_area.setWidget(self.checks)
        self.checks_area.setMaximumHeight(200)
        self.checks_area.hide()
        lay.addWidget(self.checks_area)

        self.step_label = QLabel("Preparando…")
        self.step_label.setStyleSheet("font-weight:600;")
        self.lbl_pct = QLabel("0 %")
        self.lbl_pct.setStyleSheet(f"font-weight:600; color:{theme.ACCENT};")
        head = QHBoxLayout()
        head.addWidget(self.step_label)
        head.addStretch(1)
        head.addWidget(self.lbl_pct)
        lay.addLayout(head)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        stats = QHBoxLayout()
        self.lbl_bytes = QLabel("—")
        self.lbl_speed = QLabel("—")
        self.lbl_time = QLabel("—")
        for w in (self.lbl_bytes, self.lbl_speed, self.lbl_time):
            w.setStyleSheet(f"color:{theme.MUTED};")
            stats.addWidget(w)
        stats.addStretch(1)
        lay.addLayout(stats)

        self.logview = QTextEdit()
        self.logview.setReadOnly(True)
        self.logview.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 9pt;")
        lay.addWidget(self.logview, 1)

        btns = QHBoxLayout()
        self.btn_save = QPushButton("Guardar registro…")
        self.btn_save.clicked.connect(self._save_log)
        btns.addWidget(self.btn_save)
        btns.addStretch(1)
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setObjectName("Danger")
        self.btn_cancel.clicked.connect(self._ask_cancel)
        btns.addWidget(self.btn_cancel)
        self.btn_close = QPushButton("Cerrar")
        self.btn_close.setObjectName("Primary")
        self.btn_close.setEnabled(False)
        self.btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_close)
        lay.addLayout(btns)
        root.addWidget(body, 1)

        self._done = 0
        self._total = 0
        self._speed = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        self.thread = QThread(self)
        self.worker = CloneWorker(platform, plan)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._on_log)
        self.worker.step.connect(self._on_step)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)

    def start(self):
        self._running = True
        self._t0 = time.monotonic()
        self.timer.start(500)
        self.thread.start()

    # ------------------------------------------------------------------
    def _on_log(self, msg: str, level: str):
        color = {"error": theme.DANGER, "warn": theme.WARN, "ok": theme.OK,
                 "step": theme.ACCENT}.get(level, theme.TEXT)
        weight = "600" if level in ("step", "ok", "error") else "400"
        stamp = time.strftime("%H:%M:%S")
        self.logview.append(f'<span style="color:#AEAEB2">{stamp}</span> '
                            f'<span style="color:{color}; font-weight:{weight}">{html.escape(msg)}</span>')

    def _on_step(self, idx: int, name: str):
        for i in range(self.steps.count()):
            it = self.steps.item(i)
            mark = "✓" if i < idx else ("●" if i == idx else "○")
            it.setText(f"{mark}  {STEPS[i]}")
            it.setForeground(QColor(theme.OK if i < idx else theme.ACCENT if i == idx else theme.SIDEBAR_MUTED))
        self.steps.setCurrentRow(idx)
        self.step_label.setText(name)

    def _on_progress(self, done, total, speed):
        self._done, self._total, self._speed = int(done), int(total), float(speed)
        if self._total:
            self.bar.setValue(int(1000 * self._done / self._total))
            self.lbl_pct.setText(f"{100 * self._done / self._total:.1f} %")
        self._tick()

    def _tick(self):
        elapsed = time.monotonic() - self._t0
        if self._total:
            self.lbl_bytes.setText(f"Copiado: {fmt_size(self._done)} de {fmt_size(self._total)}")
        if self._speed > 0:
            self.lbl_speed.setText(f"Velocidad: {fmt_size(self._speed)}/s")
            rem = (self._total - self._done) / self._speed if self._total else 0
            self.lbl_time.setText(f"Transcurrido: {fmt_time(elapsed)}   ·   Restante: {fmt_time(rem)}")
        else:
            self.lbl_time.setText(f"Transcurrido: {fmt_time(elapsed)}")

    def _on_finished(self, result: CloneResult):
        self.result = result
        self._running = False
        self.timer.stop()
        self._tick()
        self.thread.quit()
        self.thread.wait()
        self.btn_cancel.setEnabled(False)
        self.btn_close.setEnabled(True)
        boot_ok = result.boot_ok
        kind = ("ok" if boot_ok and not result.bad_sectors else "warn") if result.success else \
            ("warn" if result.cancelled else "error")
        text = result.message
        if result.success:
            self.bar.setValue(1000)
            self.lbl_pct.setText("100 %")
            check_step = STEPS.index("Comprobar arranque")
            for i in range(self.steps.count()):
                bad = i == check_step and not boot_ok
                self.steps.item(i).setText(f"{'✗' if bad else '✓'}  {STEPS[i]}")
                self.steps.item(i).setForeground(QColor(theme.WARN if bad else theme.OK))
            text += f"  ·  Tiempo total {fmt_time(result.elapsed)}"
            if result.warnings:
                text += f"  ·  {len(result.warnings)} advertencia(s), revisa el registro"
        self.banner.setText(text)
        self.banner.setProperty("kind", kind)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.show()
        self._show_checks(result)
        self.step_label.setText("Terminado" if result.success else "Detenido")

    def _show_checks(self, result: CloneResult):
        parts = []
        if result.boot_checks:
            rows = "".join(
                f"<div style='color:{theme.OK if ok else theme.DANGER}'>"
                f"<b>{'✓' if ok else '✗'}</b>&nbsp; {html.escape(text)}</div>"
                for ok, text in result.boot_checks)
            title = "Comprobación del arranque" if result.boot_ok else \
                "Comprobación del arranque — HAY PROBLEMAS, no instales el SSD todavía"
            parts.append(f"<b>{title}</b>{rows}")
        if result.bad_sectors:
            parts.append(f"<b style='color:{theme.WARN}'>{len(result.bad_sectors)} sector(es) "
                         "ilegibles del origen se copiaron con ceros</b> (LBA): "
                         f"{html.escape(fmt_lbas(result.bad_sectors))}")
        if parts:
            self.checks.setText("<br><br>".join(parts))
            self.checks_area.show()

    def _ask_cancel(self):
        if not self._running:
            return
        r = QMessageBox.question(
            self, "Cancelar clonación",
            "¿Seguro que quieres cancelar?\n\nEl disco destino quedará sin tabla de particiones "
            "válida y tendrás que repetir la clonación.")
        if r == QMessageBox.Yes:
            self.btn_cancel.setEnabled(False)
            self.step_label.setText("Cancelando…")
            self.worker.cancel.set()

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar registro", "clondisk_registro.txt",
                                              "Texto (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.logview.toPlainText())

    def closeEvent(self, ev):
        if self._running:
            ev.ignore()
            self._ask_cancel()
        else:
            super().closeEvent(ev)

    def reject(self):
        if self._running:
            self._ask_cancel()
        else:
            super().reject()
