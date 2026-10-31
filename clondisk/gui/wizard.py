"""Asistente de clonación paso a paso."""
from __future__ import annotations

import html
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDoubleSpinBox, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QRadioButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from ..core.model import MiB, Disk, Partition, fmt_size
from ..core.planner import (CloneOptions, ClonePlan, build_plan, estimate_minutes,
                            fmt_minutes_range, resizable_candidates, shrink_target_bytes)
from ..core.winprep import (PENDING_UPDATES_WARNING, CfaStatus, DriverBackup, assess_health,
                            cfa_banner_text, gpt_conversion_status, offline_windows,
                            source_windows)
from . import theme
from .diskmap import DiskCard, DiskMapWidget, disk_segments, plan_segments
from .progress import run_in_thread

STEP_NAMES = ["Disco de origen", "Disco de destino", "Opciones", "Particiones", "Resumen"]


def banner(text: str, kind: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Banner")
    lab.setProperty("kind", kind)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    return lab


def page_header(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    t.setWordWrap(True)
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def scroll_wrap(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    inner.setObjectName("ScrollBody")
    sa.setWidget(inner)
    return sa


def hint(text: str, color: str = "") -> QLabel:
    """Descripción con ajuste de línea bajo una casilla (las casillas no ajustan su texto)."""
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet(f"color:{color or theme.MUTED}; margin-left: 24px;")
    return lab


MIN_WIDTH, MIN_HEIGHT = 800, 560


# display_name() ya incluye «:» cuando hay letra («E:»); estos textos no le añaden otro.
def windows_label(part: Partition) -> str:
    """Texto de «Windows de origen» (HTML)."""
    info = part.win_info.display() if part.win_info else "versión no detectada"
    state = "es el Windows en ejecución" if part.is_boot else "no está en ejecución"
    return (f"<b>{html.escape(info)}</b> en {html.escape(part.display_name())} "
            f"({state}).")


def driver_backup_error_text(part: Partition, err) -> str:
    return (f"No se pudieron respaldar los drivers de {part.display_name()}.\n\n{err}\n\n"
            "¿Quieres continuar sin respaldo de drivers?")


def driver_backup_done_text(part: Partition, res: DriverBackup) -> str:
    how = "DISM /export-driver" if res.method == "dism" else \
        "copia del DriverStore (DISM no pudo procesar ese Windows)"
    text = f"Se respaldaron {res.count} driver(s) de {part.display_name()} mediante {how}.\n\n"
    if res.used_fallback_dir:
        text += ("No se pudo crear la carpeta en Documentos (suele deberse al «Acceso controlado "
                 "a carpetas» de Windows Defender), así que el respaldo se guardó en la carpeta "
                 "alternativa.\n\n")
    text += f"Carpeta:\n{res.folder}"
    if res.unmatched:
        text += f"\n\n{len(res.unmatched)} oem*.inf sin carpeta: {', '.join(res.unmatched)}"
    return (text + f"\n\nPara reinstalarlos: pnputil /add-driver \"{res.folder}\\*.inf\" "
            "/subdirs /install")


class CloneWizard(QDialog):
    def __init__(self, platform, disks: List[Disk], source: Optional[Disk] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clonar disco")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint
                            | Qt.WindowMinMaxButtonsHint)
        self.platform = platform
        self.disks = disks
        self.source: Optional[Disk] = source
        self.target: Optional[Disk] = None
        self.options = CloneOptions(use_vss=platform.supports_vss)
        self.res_index: Optional[int] = -1
        self.res_sectors: Optional[int] = None
        self.plan: Optional[ClonePlan] = None
        self._busy = False
        self.health: dict = {}                      # número de disco → DiskHealth | None
        self.driver_backup: Optional[DriverBackup] = None
        self.driver_backup_skipped = False
        self._opts_source: Optional[int] = None     # origen para el que se fijaron las opciones
        self.cfa: Optional[CfaStatus] = None        # Acceso controlado a carpetas
        self.cfa_checked = False
        self.pending: dict = {}                     # número de disco → actualizaciones pendientes
        self.cfa_override = False                   # el usuario confirmó seguir sin permitirlo
        self._preflight_running = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.step_list = QListWidget()
        self.step_list.setObjectName("StepList")
        self.step_list.setFixedWidth(220)
        self.step_list.setFocusPolicy(Qt.NoFocus)
        self.step_list.setSelectionMode(QAbstractItemView.SingleSelection)
        for i, n in enumerate(STEP_NAMES, 1):
            QListWidgetItem(f"{i}.  {n}", self.step_list)
        self.step_list.itemClicked.connect(lambda _it: self.step_list.setCurrentRow(self.stack.currentIndex()))
        root.addWidget(self.step_list)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(22, 18, 22, 14)
        self.stack = QStackedWidget()
        rl.addWidget(self.stack, 1)
        # La navegación queda fuera de las páginas (que tienen scroll): siempre visible.
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{theme.BORDER};")
        rl.addWidget(sep)
        nav = QHBoxLayout()
        nav.addStretch(1)
        self.btn_back = QPushButton("< Atrás")
        self.btn_next = QPushButton("Siguiente >")
        self.btn_next.setObjectName("Primary")
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_back.clicked.connect(self.go_back)
        self.btn_next.clicked.connect(self.go_next)
        self.btn_cancel.clicked.connect(self.reject)
        for b in (self.btn_back, self.btn_next, self.btn_cancel):
            nav.addWidget(b)
        rl.addLayout(nav)
        root.addWidget(right, 1)

        self.stack.addWidget(self._build_source_page())
        self.stack.addWidget(self._build_target_page())
        self.stack.addWidget(self._build_options_page())
        self.stack.addWidget(self._build_layout_page())
        self.stack.addWidget(self._build_summary_page())
        self.stack.currentChanged.connect(self._fit_stack_to_page)
        self.show_page(0)
        self._fit_to_screen()

    def _fit_to_screen(self):
        screen = self.screen() or QGuiApplication.primaryScreen()
        mw, mh = MIN_WIDTH, MIN_HEIGHT
        if screen is not None:
            avail = screen.availableGeometry()
            mw, mh = min(mw, avail.width() - 20), min(mh, avail.height() - 50)
        self.setMinimumSize(mw, mh)
        theme.fit_to_screen(self, 1080, 760)

    def _fit_stack_to_page(self, index: int):
        """Solo la página visible cuenta para el tamaño: las ocultas no imponen su altura."""
        for i in range(self.stack.count()):
            pol = QSizePolicy.Preferred if i == index else QSizePolicy.Ignored
            self.stack.widget(i).setSizePolicy(pol, pol)
        page = self.stack.widget(index)
        if page is not None:
            page.adjustSize()

    # ------------------------------------------------------------------ navegación
    def show_page(self, i: int):
        self.stack.setCurrentIndex(i)
        self.step_list.setCurrentRow(i)
        for j in range(self.step_list.count()):
            it = self.step_list.item(j)
            it.setForeground(QColor(theme.ACCENT if j == i else theme.SIDEBAR_TEXT if j < i else theme.SIDEBAR_MUTED))
        if i == 0:
            self._refresh_source_page()
        elif i == 1:
            self._refresh_target_page()
        elif i == 2:
            self._refresh_options_page()
        elif i == 3:
            self._rebuild_plan(reset_size=False)
            self._fetch_health()
        elif i == 4:
            self._refresh_summary()
            self._fetch_health(then=self._fetch_preflight)
        self.btn_back.setEnabled(i > 0)
        self.btn_next.setText("Iniciar clonación" if i == 4 else "Siguiente >")
        self.btn_next.setObjectName("Danger" if i == 4 else "Primary")
        self.btn_next.style().unpolish(self.btn_next)
        self.btn_next.style().polish(self.btn_next)
        self._update_next()

    def _update_next(self):
        i = self.stack.currentIndex()
        ok = {
            0: self.source is not None,
            1: self.target is not None,
            2: True,
            3: bool(self.plan and self.plan.ok),
            4: bool(self.plan and self.plan.ok and self.confirm.isChecked()
                    and (self.plan.options.dry_run or not self._cfa_blocks())),
        }[i]
        self.btn_next.setEnabled(ok and not self._busy)

    def go_back(self):
        if self.stack.currentIndex() > 0:
            self.show_page(self.stack.currentIndex() - 1)

    def go_next(self):
        i = self.stack.currentIndex()
        if i == 2:
            self._read_options()
        if i < 4:
            self.show_page(i + 1)
        elif self.options.dry_run:
            self.accept()
        else:
            self._with_driver_backup(self.accept)

    # ------------------------------------------------------------------ tareas en segundo plano
    def _run_busy(self, work, on_done):
        def done(res, err):
            self._busy = False
            self.setCursor(Qt.ArrowCursor)
            self._update_next()
            on_done(res, err)

        self._busy = True
        self.setCursor(Qt.WaitCursor)
        self._update_next()
        run_in_thread(self, work, on_done=done)

    def _fetch_health(self, then=None):
        """Consulta (una vez por disco) la salud del origen y vuelve a pintar los avisos."""
        src = self.source
        if src is None or src.number in self.health:
            if then:
                then()
            return

        def work():
            try:
                return self.platform.disk_health(src)
            except Exception:
                return None

        def done(res, _err):
            self.health[src.number] = res
            if self.stack.currentIndex() == 3:
                self._rebuild_plan()
            elif self.stack.currentIndex() == 4:
                self._refresh_summary(keep_confirm=True)
            if then:
                then()

        self._run_busy(work, done)

    # ------------------------------------------------------------------ comprobaciones previas
    def _cfa_blocks(self) -> bool:
        return bool(self.cfa and self.cfa.blocks and not self.cfa_override)

    def _fetch_preflight(self):
        """Antes de clonar: ¿bloqueará el Acceso controlado a carpetas la escritura? ¿Tiene
        el Windows de origen actualizaciones pendientes?"""
        src = self.source
        win = source_windows(src) if src else None
        need_cfa = not self.cfa_checked
        need_pending = win is not None and src.number not in self.pending
        if self._preflight_running or not (need_cfa or need_pending):
            return
        self._preflight_running = True

        def work():
            cfa = pend = None
            if need_cfa:
                try:
                    cfa = self.platform.controlled_folder_access()
                except Exception:
                    cfa = None
            if need_pending:
                try:
                    pend = self.platform.pending_updates(win)
                except Exception:
                    pend = None
            return cfa, pend

        def done(res, _err):
            self._preflight_running = False
            cfa, pend = res or (None, None)
            if need_cfa:
                self.cfa_checked, self.cfa = True, cfa
            if need_pending:
                self.pending[src.number] = pend
                if pend and self.stack.currentIndex() == 4:
                    self._refresh_summary(keep_confirm=True)
            self._update_cfa_box()

        self._run_busy(work, done)

    def _pending_reasons(self) -> List[str]:
        return (self.pending.get(self.source.number) or []) if self.source else []

    def _update_cfa_box(self):
        st = self.cfa
        show = bool(st and st.blocks)
        self.cfa_box.setVisible(show)
        if show:
            text = html.escape(cfa_banner_text(st))
            if self.cfa_override:
                text += ("<br><b>Elegiste continuar sin permitirlo:</b> lo más probable es que "
                         "Windows deniegue la escritura y la clonación se detenga al empezar.")
            self.cfa_label.setText("<b>⚠ Acceso controlado a carpetas</b><br>" + text)
            self.btn_cfa_skip.setVisible(not self.cfa_override)
        self._update_next()

    def _cfa_allow(self):
        exe = self.cfa.exe if self.cfa else ""
        r = QMessageBox.question(
            self, "Permitir ClonDisk",
            "Se agregará esta aplicación a las permitidas del «Acceso controlado a carpetas» de "
            f"Windows Defender:\n\n{exe}\n\nEquivale a ejecutar:\nAdd-MpPreference "
            f"-ControlledFolderAccessAllowedApplications \"{exe}\"\n\n¿Continuar?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return

        def work():
            self.platform.allow_controlled_folder_access(exe)
            return self.platform.controlled_folder_access()

        def done(res, err):
            if err:
                QMessageBox.critical(
                    self, "Permitir ClonDisk",
                    f"No se pudo agregar la excepción: {err}\n\nHazlo a mano: Seguridad de Windows → "
                    "Protección contra virus y amenazas → Administrar la protección contra "
                    "ransomware → Permitir una aplicación mediante Acceso controlado a carpetas.")
                return
            self.cfa = res
            if res and res.blocks:
                QMessageBox.warning(
                    self, "Permitir ClonDisk",
                    "Windows todavía no muestra ClonDisk como aplicación permitida. Puede que una "
                    "directiva de la organización controle esta opción; agrégala a mano o "
                    "consulta con quien administra el equipo.")
            self._update_cfa_box()

        self._run_busy(work, done)

    def _cfa_skip(self):
        r = QMessageBox.warning(
            self, "Continuar sin permitir ClonDisk",
            "Si continúas sin permitir ClonDisk, lo más probable es que Windows deniegue la "
            "escritura en el disco destino y la clonación se detenga al empezar (el origen no se "
            "modifica).\n\n¿Continuar de todos modos?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self.cfa_override = True
            self._update_cfa_box()

    def _health_html(self) -> str:
        if self.source is None or self.source.number not in self.health:
            return ""
        level, msgs = assess_health(self.health[self.source.number])
        body = "<br>".join(html.escape(m) for m in msgs)
        if level == "danger":
            return (f"<b>⚠ ADVERTENCIA: el disco de origen puede estar fallando.</b><br>{body}<br>"
                    "Respalda primero lo más importante, activa «Saltar sectores ilegibles» y "
                    "«Verificar», y evita reducir particiones en este disco si no es imprescindible.")
        return f"<b>Salud del disco de origen:</b> {body}"

    def _with_driver_backup(self, cont):
        """Respalda los drivers del Windows de origen (una sola vez) y luego ejecuta ``cont``.
        Si el respaldo falla, el usuario decide si sigue."""
        part = offline_windows(self.source) if self.source else None
        if (not self.options.backup_drivers or part is None or self.driver_backup is not None
                or self.driver_backup_skipped or not self.platform.supports_offline_windows):
            cont()
            return
        notes: List[str] = []

        def work():
            return self.platform.backup_drivers(part, lambda m, l="info": notes.append(m))

        def done(res, err):
            if err:
                r = QMessageBox.question(
                    self, "Respaldo de drivers", driver_backup_error_text(part, err),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if r == QMessageBox.Yes:
                    self.driver_backup_skipped = True
                    cont()
                return
            self.driver_backup = res
            QMessageBox.information(self, "Respaldo de drivers", driver_backup_done_text(part, res))
            cont()

        self._run_busy(work, done)

    # ------------------------------------------------------------------ página 1: origen
    def _build_source_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page_header("Selecciona el disco de origen",
                                  "Es el disco que quieres copiar (por ejemplo, tu HDD actual con Windows). "
                                  "Solo se modifica si decides reducir una de sus particiones "
                                  "para que quepa en el destino."))
        self.src_body = QWidget()
        self.src_layout = QVBoxLayout(self.src_body)
        self.src_layout.setSpacing(10)
        lay.addWidget(scroll_wrap(self.src_body), 1)
        return w

    def _clear(self, layout):
        while layout.count():
            it = layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _refresh_source_page(self):
        self._clear(self.src_layout)
        self.src_cards = []
        for d in self.disks:
            reason = "" if d.style in ("GPT", "MBR") else "Disco sin particiones reconocibles"
            card = DiskCard(d, selectable=True, disabled_reason=reason)
            card.clicked.connect(self._pick_source)
            self.src_layout.addWidget(card)
            self.src_cards.append(card)
            if self.source and d.number == self.source.number:
                card.set_selected(True)
        self.src_layout.addStretch(1)

    def _pick_source(self, disk: Disk):
        if self.source is None or self.source.number != disk.number:
            self.target = None
            self.res_index, self.res_sectors = -1, None
        self.source = disk
        for c in self.src_cards:
            c.set_selected(c.disk.number == disk.number)
        self._update_next()

    # ------------------------------------------------------------------ página 2: destino
    def _build_target_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page_header("Selecciona el disco de destino",
                                  "Normalmente tu SSD nuevo. Todo su contenido actual será reemplazado."))
        self.tgt_warn = banner("", "error")
        self.tgt_warn.hide()
        lay.addWidget(self.tgt_warn)
        self.tgt_body = QWidget()
        self.tgt_layout = QVBoxLayout(self.tgt_body)
        self.tgt_layout.setSpacing(10)
        lay.addWidget(scroll_wrap(self.tgt_body), 1)
        return w

    def _target_reason(self, d: Disk) -> str:
        if self.source and d.number == self.source.number:
            return "Es el disco de origen"
        if d.is_running_system:
            return "Contiene el Windows en ejecución: no puede ser destino"
        if self.source and d.sector_size != self.source.sector_size:
            return (f"Sector lógico de {d.sector_size} B (el origen usa {self.source.sector_size} B). "
                    "Si usas un adaptador USB, prueba con otro o conéctalo por SATA/M.2.")
        return ""

    def _refresh_target_page(self):
        self._clear(self.tgt_layout)
        self.tgt_cards = []
        for d in self.disks:
            card = DiskCard(d, selectable=True, disabled_reason=self._target_reason(d))
            card.clicked.connect(self._pick_target)
            self.tgt_layout.addWidget(card)
            self.tgt_cards.append(card)
            if self.target and d.number == self.target.number:
                card.set_selected(True)
        self.tgt_layout.addStretch(1)
        self._update_target_warning()

    def _pick_target(self, disk: Disk):
        if self.target is None or self.target.number != disk.number:
            self.res_sectors = None
        self.target = disk
        for c in self.tgt_cards:
            c.set_selected(c.disk.number == disk.number)
        self._update_target_warning()
        self._update_next()

    def _update_target_warning(self):
        if not self.target:
            self.tgt_warn.hide()
            return
        d = self.target
        n_parts = len(d.partitions)
        extra = f" Tiene {n_parts} partición(es) que se perderán." if n_parts else ""
        self.tgt_warn.setText(f"<b>Atención:</b> se borrará TODO el contenido del Disco {d.number} "
                              f"({html.escape(d.model)}, {fmt_size(d.size_bytes)}).{extra}")
        self.tgt_warn.show()

    # ------------------------------------------------------------------ página 3: opciones
    def _build_options_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page_header("Opciones de clonación",
                                  "Los valores predeterminados son los recomendados para pasar de HDD a SSD."))
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 8, 0)
        lay.addWidget(scroll_wrap(body), 1)

        g1 = QGroupBox("Copia")
        l1 = QVBoxLayout(g1)
        self.chk_smart = QCheckBox("Copia inteligente")
        self.chk_vss = QCheckBox("Usar instantáneas VSS")
        self.chk_verify = QCheckBox("Verificar los datos copiados")
        self.lbl_verify_hint = hint("Recomendado: el origen contiene Windows; verificar la copia "
                                    "detecta errores de lectura del HDD o del adaptador USB.", theme.WARN)
        self.chk_skip_bad = QCheckBox("Saltar sectores ilegibles")
        for x in (self.chk_smart,
                  hint("Copia solo los sectores en uso de NTFS (mucho más rápido)."),
                  self.chk_vss, hint("Permite clonar Windows mientras está en uso."),
                  self.chk_verify, hint("Duplica el tiempo de la copia."), self.lbl_verify_hint,
                  self.chk_skip_bad,
                  hint("Los copia con ceros y anota su LBA. Si no se marca, la copia se detiene "
                       "en el primero.")):
            l1.addWidget(x)
        self.chk_vss.setEnabled(self.platform.supports_vss)
        bl.addWidget(g1)

        self.g_win = QGroupBox("Windows de origen")
        lw = QVBoxLayout(self.g_win)
        self.lbl_win = QLabel()
        self.lbl_win.setWordWrap(True)
        self.chk_drivers = QCheckBox("Respaldar drivers del Windows de origen")
        for x in (self.lbl_win, self.chk_drivers,
                  hint("Se guardan en Documentos\\ClonDisk antes de reducir o clonar.")):
            lw.addWidget(x)
        bl.addWidget(self.g_win)

        g2 = QGroupBox("Identificadores del disco y arranque")
        l2 = QVBoxLayout(g2)
        self.rb_new = QRadioButton("Nuevos identificadores + reparar arranque (recomendado)")
        d1 = hint("El SSD recibe GUID/firma nuevos, se reconstruye el arranque con bcdboot y se "
                  "actualizan las letras de unidad del Windows clonado. Puedes dejar ambos discos "
                  "conectados sin conflictos y las particiones quedan alineadas a 1 MiB.")
        self.rb_same = QRadioButton("Identificadores idénticos (clon exacto)")
        d2 = hint("El SSD conserva los mismos GUID/firma que el origen. Al terminar queda offline "
                  "para evitar colisiones: debes retirar el disco de origen antes de arrancar.")
        self.chk_boot = QCheckBox("Reconstruir el arranque y MountedDevices")
        self.chk_boot.setStyleSheet("margin-left: 24px;")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_new)
        grp.addButton(self.rb_same)
        self.rb_new.toggled.connect(lambda on: self.chk_boot.setEnabled(on))
        for x in (self.rb_new, d1, self.chk_boot, self.rb_same, d2):
            l2.addWidget(x)
        bl.addWidget(g2)

        self.g_fw = QGroupBox("Formato de arranque del destino")
        lf = QVBoxLayout(self.g_fw)
        self.rb_keep_mbr = QRadioButton("Igual que el origen (MBR/BIOS)")
        self.rb_to_gpt = QRadioButton("Convertir a GPT/UEFI (recomendado para equipos modernos)")
        self.lbl_fw = hint("")
        grp_fw = QButtonGroup(self)
        for x in (self.rb_keep_mbr, self.rb_to_gpt):
            grp_fw.addButton(x)
        for x in (self.rb_keep_mbr, self.rb_to_gpt, self.lbl_fw):
            lf.addWidget(x)
        self.rb_new.toggled.connect(lambda _on: self._update_fw_controls())
        self.chk_boot.toggled.connect(lambda _on: self._update_fw_controls())
        bl.addWidget(self.g_fw)

        g3 = QGroupBox("Al terminar")
        l3 = QVBoxLayout(g3)
        self.chk_trim = QCheckBox("Enviar TRIM al SSD")
        self.chk_dry = QCheckBox("Simulación")
        self.chk_newhw = QCheckBox("El SSD irá a otra computadora")
        for x in (self.chk_trim, hint("Optimiza el rendimiento del SSD desde el principio."),
                  self.chk_newhw,
                  hint("Habilita los drivers de almacenamiento AHCI, NVMe e Intel RST del Windows "
                       "clonado. Evita la pantalla azul INACCESSIBLE_BOOT_DEVICE con otra "
                       "controladora. Windows y el software con licencia (p. ej. el del plotter) "
                       "pueden pedir reactivación por el cambio de hardware."),
                  self.chk_dry, hint("Analiza y valida todo sin escribir en el destino.")):
            l3.addWidget(x)
        bl.addWidget(g3)
        bl.addStretch(1)

        o = self.options
        self.chk_smart.setChecked(o.smart_copy)
        self.chk_vss.setChecked(o.use_vss)
        self.chk_verify.setChecked(o.verify)
        self.rb_new.setChecked(o.id_mode == "new")
        self.rb_same.setChecked(o.id_mode == "same")
        self.chk_boot.setChecked(o.fix_boot)
        self.chk_trim.setChecked(o.retrim)
        self.chk_dry.setChecked(o.dry_run)
        self.chk_skip_bad.setChecked(o.skip_bad_sectors)
        self.chk_newhw.setChecked(o.new_hardware)
        self.rb_keep_mbr.setChecked(not o.convert_to_gpt)
        self.rb_to_gpt.setChecked(o.convert_to_gpt)
        return w

    def _conversion_status(self):
        allowed, reason = gpt_conversion_status(self.source, self.platform.host_build(),
                                                self.platform.has_mbr2gpt())
        if allowed and not self.rb_new.isChecked():
            return False, "Requiere «Nuevos identificadores + reparar arranque»."
        if allowed and not self.chk_boot.isChecked():
            return False, "Requiere «Reconstruir el arranque» (bcdboot)."
        return allowed, reason

    def _update_fw_controls(self):
        if self.source is None or self.source.style != "MBR":
            return
        allowed, reason = self._conversion_status()
        self.rb_to_gpt.setEnabled(allowed)
        if not allowed and self.rb_to_gpt.isChecked():
            self.rb_keep_mbr.setChecked(True)
        if allowed:
            self.lbl_fw.setText("Con MBR el SSD arrancará solo en modo BIOS/Legacy (CSM). Al convertir, "
                                "se ejecuta mbr2gpt al terminar y el SSD arrancará en modo UEFI.")
        else:
            self.lbl_fw.setText("Conversión no disponible: " + reason)

    def _refresh_options_page(self):
        src = self.source
        if src is None:
            return
        win = source_windows(src)
        offline = offline_windows(src)
        self.lbl_verify_hint.setVisible(win is not None and not self.chk_verify.isChecked())
        self.g_win.setVisible(win is not None)
        if win is not None:
            self.lbl_win.setText(windows_label(win))
        can_backup = offline is not None and self.platform.supports_offline_windows
        self.chk_drivers.setEnabled(can_backup)
        if self._opts_source != src.number:
            # Valor por defecto según el origen; después se respeta lo que elija el usuario.
            self._opts_source = src.number
            self.chk_drivers.setChecked(can_backup)
            self.rb_keep_mbr.setChecked(True)
            self.driver_backup, self.driver_backup_skipped = None, False
        if not can_backup:
            self.chk_drivers.setChecked(False)
        self.g_fw.setVisible(src.style == "MBR")
        self._update_fw_controls()

    def _read_options(self):
        self.options = CloneOptions(
            smart_copy=self.chk_smart.isChecked(),
            use_vss=self.chk_vss.isChecked() and self.platform.supports_vss,
            verify=self.chk_verify.isChecked(),
            id_mode="new" if self.rb_new.isChecked() else "same",
            fix_boot=self.chk_boot.isChecked(),
            retrim=self.chk_trim.isChecked(),
            dry_run=self.chk_dry.isChecked(),
            skip_bad_sectors=self.chk_skip_bad.isChecked(),
            backup_drivers=self.chk_drivers.isChecked() and self.chk_drivers.isEnabled(),
            convert_to_gpt=(self.source is not None and self.source.style == "MBR"
                            and self.rb_to_gpt.isChecked() and self.rb_to_gpt.isEnabled()),
            new_hardware=self.chk_newhw.isChecked(),
        )

    # ------------------------------------------------------------------ página 4: particiones
    def _build_layout_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(page_header("Distribución de particiones en el destino",
                                  "Las particiones de sistema conservan su tamaño; la partición de datos "
                                  "se ajusta al espacio del SSD. Todo queda alineado a 1 MiB."))
        body = QWidget()
        outer = lay
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(8)
        outer.addWidget(scroll_wrap(body), 1)
        self.lbl_src = QLabel()
        self.lbl_src.setWordWrap(True)
        self.lbl_src.setStyleSheet("font-weight:600;")
        lay.addWidget(self.lbl_src)
        self.map_src = DiskMapWidget()
        lay.addWidget(self.map_src)
        self.lbl_dst = QLabel()
        self.lbl_dst.setWordWrap(True)
        self.lbl_dst.setStyleSheet("font-weight:600;")
        lay.addWidget(self.lbl_dst)
        self.map_dst = DiskMapWidget()
        lay.addWidget(self.map_dst)

        ctl = QFrame()
        ctl.setObjectName("Card")
        cl = QVBoxLayout(ctl)
        row = QHBoxLayout()
        row.addWidget(QLabel("Partición a ajustar:"))
        self.cmb_res = QComboBox()
        self.cmb_res.setMinimumWidth(260)
        self.cmb_res.currentIndexChanged.connect(self._on_res_changed)
        row.addWidget(self.cmb_res)
        row.addStretch(1)
        cl.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Tamaño:"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.valueChanged.connect(self._on_slider)
        row2.addWidget(self.slider, 1)
        self.spin = QDoubleSpinBox()
        self.spin.setSuffix(" GB")
        self.spin.setDecimals(2)
        self.spin.setKeyboardTracking(False)
        self.spin.valueChanged.connect(self._on_spin)
        row2.addWidget(self.spin)
        b_min = QPushButton("Original")
        b_max = QPushButton("Máximo")
        b_min.clicked.connect(lambda: self.slider.setValue(self.slider.minimum()))
        b_max.clicked.connect(lambda: self.slider.setValue(self.slider.maximum()))
        row2.addWidget(b_min)
        row2.addWidget(b_max)
        cl.addLayout(row2)
        lay.addWidget(ctl)

        self.msg_box = QVBoxLayout()
        lay.addLayout(self.msg_box)
        self.btn_shrink = QPushButton("Reducir partición de origen…")
        self.btn_shrink.clicked.connect(self._shrink_source)
        self.btn_shrink.hide()
        lay.addWidget(self.btn_shrink, 0, Qt.AlignLeft)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Partición", "Tipo", "Sistema", "Tamaño origen",
                                              "Tamaño destino", "Método de copia"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setMinimumHeight(170)
        lay.addWidget(self.table, 1)
        return w

    def _rebuild_plan(self, reset_size: bool = False):
        if not (self.source and self.target):
            return
        if reset_size:
            self.res_sectors = None
        self.plan = build_plan(self.source, self.target, self.options, self.res_index, self.res_sectors)
        p = self.plan
        self.lbl_src.setText(f"Origen: {self.source.title()}")
        self.lbl_dst.setText(f"Destino (resultado): {self.target.title()}")
        self.map_src.set_segments(disk_segments(self.source))
        self.map_dst.set_segments(plan_segments(p) if p.parts else disk_segments(self.target))

        # combo de partición redimensionable
        self.cmb_res.blockSignals(True)
        self.cmb_res.clear()
        self.cmb_res.addItem("Ninguna (mantener tamaños originales)", None)
        for c in resizable_candidates(self.source):
            self.cmb_res.addItem(f"{c.display_name()}  ({fmt_size(c.size_bytes(self.source.sector_size))})", c.index)
        idx = self.cmb_res.findData(p.resizable_index)
        self.cmb_res.setCurrentIndex(max(0, idx))
        self.cmb_res.blockSignals(False)

        ss = self.source.sector_size
        has_res = p.resizable_index is not None and p.resize_max_sectors > 0 and p.ok
        for wdg in (self.slider, self.spin):
            wdg.blockSignals(True)
            wdg.setEnabled(has_res)
        if has_res:
            mn = (p.resize_min_sectors * ss) // MiB
            mx = (p.resize_max_sectors * ss) // MiB
            self.slider.setRange(mn, mx)
            pp = p.by_src_index(p.resizable_index)
            cur = (pp.new_sectors * ss) // MiB if pp else mx
            self.slider.setValue(cur)
            self.spin.setRange(mn / 1024, mx / 1024)
            self.spin.setValue(cur / 1024)
        for wdg in (self.slider, self.spin):
            wdg.blockSignals(False)

        # mensajes
        self._clear(self.msg_box)
        health = self._health_html()
        if health:
            level = assess_health(self.health.get(self.source.number))[0]
            self.msg_box.addWidget(banner(health, "error" if level == "danger" else "info"))
        for e in p.errors:
            self.msg_box.addWidget(banner("<b>No se puede clonar:</b> " + html.escape(e), "error"))
        for wmsg in p.warnings:
            self.msg_box.addWidget(banner(html.escape(wmsg), "warn"))
        if p.ok:
            boot = ("<b>BIOS → UEFI</b> (mbr2gpt al final)" if p.converts_to_gpt
                    else f"<b>{p.firmware}</b>")
            self.msg_box.addWidget(banner(
                f"Se copiarán aprox. <b>{fmt_size(p.estimated_copy_bytes())}</b>. "
                f"Arranque: {boot}.", "info"))
        self.btn_shrink.setVisible(bool(p.deficit_sectors and p.resizable_index is not None
                                        and not self.platform.is_lab))

        # tabla
        rows = p.parts if p.parts else []
        src_parts = self.source.partitions if not rows else [pp.src for pp in rows]
        self.table.setRowCount(len(src_parts))
        for r, part in enumerate(src_parts):
            pp = rows[r] if rows else None
            vals = [part.display_name(), part.type_name, part.fs, fmt_size(part.size_bytes(ss)),
                    fmt_size(pp.new_sectors * ss) if pp else "—",
                    ("Inteligente (sectores usados)" if pp and pp.copy_mode == "smart" else "Sector a sector")
                    if pp else "—"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if pp and c == 4 and pp.new_sectors != part.sectors:
                    it.setForeground(QColor(theme.OK))
                self.table.setItem(r, c, it)
        self._update_next()

    def _on_res_changed(self, _i):
        self.res_index = self.cmb_res.currentData()
        self.res_sectors = None
        self._rebuild_plan()

    def _on_slider(self, v: int):
        self.spin.blockSignals(True)
        self.spin.setValue(v / 1024)
        self.spin.blockSignals(False)
        self._apply_size(v)

    def _on_spin(self, gb: float):
        v = int(round(gb * 1024))
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self._apply_size(v)

    def _apply_size(self, mib: int):
        ss = self.source.sector_size
        if mib >= self.slider.maximum():
            self.res_sectors = None
        else:
            self.res_sectors = (mib * MiB) // ss
        self._rebuild_plan()

    def _shrink_source(self):
        p = self.plan
        part = self.source.partition_by_index(p.resizable_index)
        new_size = shrink_target_bytes(p)
        if part is None or new_size is None:
            return
        # Primero el respaldo de drivers: después se modifica el origen.
        self._with_driver_backup(lambda: self._shrink_query(part, new_size))

    def _shrink_query(self, part: Partition, new_size: int, note: str = ""):
        src = self.source

        def work():
            mn = self.platform.min_partition_size(src, part)
            try:
                health = self.platform.disk_health(src)
            except Exception:
                health = None
            return mn, health

        def done(res, err):
            if err:
                QMessageBox.critical(self, "Reducir partición de origen",
                                     f"No se pudo consultar el tamaño mínimo: {err}")
                return
            mn, health = res
            self.health[src.number] = health
            self._shrink_dialog(part, new_size, mn, note)

        self._run_busy(work, done)

    def _shrink_dialog(self, part: Partition, new_size: int, mn: Optional[int], note: str = ""):
        src, ss = self.source, self.source.sector_size
        e = html.escape
        name = e(part.display_name())
        offline = part.has_windows and not part.is_boot
        too_small = bool(mn and new_size < mn)
        health = self._health_html()
        level = assess_health(self.health.get(src.number))[0]
        rows = [("Tamaño actual", fmt_size(part.size_bytes(ss))),
                ("Tamaño necesario para caber en el destino", fmt_size(new_size)),
                ("Mínimo que permite Windows ahora", fmt_size(mn) if mn else "desconocido")]
        table = "".join(f"<tr><td>{a}</td><td align='right'>&nbsp;&nbsp;<b>{b}</b></td></tr>"
                        for a, b in rows)
        text = (f"{note}<p><b>Se va a MODIFICAR la partición {name} del disco de ORIGEN</b> "
                f"(Disco {src.number}, {e(src.model)}). Haz un respaldo de tus datos importantes "
                "antes de continuar.</p>"
                f"<table>{table}</table>")
        if health:
            color = theme.DANGER if level == "danger" else theme.MUTED
            text += f"<p style='color:{color}'>{health}</p>"
        box = QMessageBox(self)
        box.setWindowTitle("Reducir partición de origen")
        box.setTextFormat(Qt.RichText)
        box.setIcon(QMessageBox.Warning)
        if too_small and offline:
            letter = e(part.letter or "?")
            text += (
                f"<p><b>Windows no puede reducir {name} hasta el tamaño necesario.</b> Ese Windows "
                "no está en ejecución, así que comandos como <code>powercfg /h off</code> solo "
                "afectarían a este equipo, no a él. Lo que suele impedirlo en un Windows sin conexión:</p><ul>"
                f"<li><code>{letter}:\\hiberfil.sys</code>, <code>{letter}:\\pagefile.sys</code> y "
                f"<code>{letter}:\\swapfile.sys</code> (ese Windows los vuelve a crear al arrancar);</li>"
                f"<li>las instantáneas / puntos de restauración de {letter}: "
                f"(<code>vssadmin delete shadows /for={letter}: /quiet</code>);</li>"
                f"<li>archivos del sistema al final del volumen: después de limpiar, ejecuta "
                f"<code>defrag {letter}: /X</code> para consolidar el espacio libre.</li></ul>"
                "<p>«Preparar volumen para reducir» hace los dos primeros pasos y vuelve a consultar "
                "el mínimo.</p>")
            box.setText(text)
            b_prep = box.addButton("Preparar volumen para reducir…", QMessageBox.AcceptRole)
            box.addButton("Cerrar", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is b_prep:
                self._prepare_volume(part, new_size)
            return
        if too_small:
            text += (f"<p><b>Windows solo puede reducir la partición hasta {fmt_size(mn)}</b> por "
                     "archivos inamovibles. Desactiva temporalmente la hibernación (powercfg /h off), "
                     "el archivo de paginación y los puntos de restauración, reinicia y vuelve a "
                     "intentarlo.</p>")
            box.setText(text)
            box.addButton("Cerrar", QMessageBox.RejectRole)
            box.exec()
            return
        text += ("<p>Windows lo hace de forma segura (igual que «Reducir volumen» en Administración "
                 "de discos).</p><p>¿Reducir ahora?</p>")
        box.setText(text)
        b_yes = box.addButton("Reducir partición de origen", QMessageBox.AcceptRole)
        box.addButton("Cancelar", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is b_yes:
            self._do_shrink(part, new_size)

    def _prepare_volume(self, part: Partition, new_size: int):
        letter = part.letter or "?"
        r = QMessageBox.warning(
            self, "Preparar volumen para reducir",
            f"Se modificará el volumen {letter}: del disco de ORIGEN (Disco {self.source.number}):\n\n"
            f"• Se borrarán {letter}:\\hiberfil.sys, {letter}:\\pagefile.sys y {letter}:\\swapfile.sys.\n"
            f"• Se eliminarán todas las instantáneas y puntos de restauración de {letter}:.\n\n"
            "Ese Windows recrea esos archivos al arrancar. Si quedó hibernado (o con «inicio rápido»), "
            "perderá la sesión guardada y arrancará en frío. Los programas, drivers y datos no se tocan.\n\n"
            "¿Continuar?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        src = self.source

        def work():
            return self.platform.prepare_offline_volume(src, part, lambda *a: None)

        def done(actions, err):
            if err:
                QMessageBox.critical(self, "Preparar volumen", f"No se pudo preparar {letter}: {err}")
                return
            note = ("<p><b>Volumen preparado:</b><br>"
                    + "<br>".join(html.escape(a) for a in actions) + "</p>")
            self._shrink_query(part, new_size, note)

        self._run_busy(work, done)

    def _do_shrink(self, part: Partition, new_size: int):
        def work():
            self.platform.shrink_partition(self.source, part, new_size, lambda *a: None)
            return self.platform.list_disks()

        def done(disks, err):
            if err:
                QMessageBox.critical(self, "No se pudo reducir", str(err))
            else:
                self.disks = disks
                self.source = next(d for d in disks if d.number == self.source.number)
                self.target = next(d for d in disks if d.number == self.target.number)
            self._rebuild_plan(reset_size=True)

        self._run_busy(work, done)

    # ------------------------------------------------------------------ página 5: resumen
    def _build_summary_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page_header("Resumen", "Revisa las operaciones antes de iniciar."))
        self.cfa_box = QWidget()
        cl = QVBoxLayout(self.cfa_box)
        cl.setContentsMargins(0, 0, 0, 6)
        self.cfa_label = banner("", "error")
        cl.addWidget(self.cfa_label)
        row = QHBoxLayout()
        self.btn_cfa_allow = QPushButton("Permitir ClonDisk")
        self.btn_cfa_allow.setObjectName("Primary")
        self.btn_cfa_allow.clicked.connect(self._cfa_allow)
        self.btn_cfa_skip = QPushButton("Continuar sin permitir…")
        self.btn_cfa_skip.clicked.connect(self._cfa_skip)
        row.addWidget(self.btn_cfa_allow)
        row.addWidget(self.btn_cfa_skip)
        row.addStretch(1)
        cl.addLayout(row)
        self.cfa_box.hide()
        lay.addWidget(self.cfa_box)
        # QTextBrowser ya es un área con scroll; la casilla de confirmación queda fija
        # debajo, junto a la navegación.
        self.summary = QTextBrowser()
        self.summary.setMinimumHeight(120)
        lay.addWidget(self.summary, 1)
        self.confirm = QCheckBox()
        self.confirm.toggled.connect(lambda _: self._update_next())
        self.confirm.setStyleSheet("font-weight:600;")
        lay.addWidget(self.confirm)
        return w

    def _refresh_summary(self, keep_confirm: bool = False):
        p = self.plan
        if p is None:
            return
        ss = p.sector_size
        o = p.options
        e = html.escape
        ops = []
        win = source_windows(p.source)
        if o.backup_drivers and not o.dry_run:
            if self.driver_backup:
                ops.append(f"Drivers del Windows de origen ya respaldados ({self.driver_backup.count}) "
                           f"en <code>{e(self.driver_backup.folder)}</code>")
            else:
                ops.append("Respaldar los drivers del Windows de origen en Documentos\\ClonDisk "
                           "(antes de empezar)")
        ops.append(f"Poner el Disco {p.target.number} offline y borrar su tabla de particiones")
        if o.use_vss:
            ops.append("Crear instantáneas VSS de los volúmenes NTFS montados del origen")
        for pp in p.parts:
            s = pp.src
            how = "copia inteligente" if pp.copy_mode == "smart" else "sector a sector"
            used = f", {fmt_size(s.used_bytes)} en uso" if s.used_bytes is not None and pp.copy_mode == "smart" else ""
            line = (f"Copiar <b>{e(s.display_name())}</b> ({e(s.fs)}, {fmt_size(s.size_bytes(ss))}{used}) "
                    f"— {how} → inicio {fmt_size(pp.new_start_lba * ss)}")
            if pp.new_sectors != s.sectors:
                line += f"; ampliar a <b>{fmt_size(pp.new_sectors * ss)}</b>"
            ops.append(line)
        if o.verify:
            ops.append("Verificar todos los datos copiados")
        ops.append(f"Escribir tabla {p.source.style} "
                   + ("con identificadores nuevos" if o.id_mode == "new" else "con identificadores temporales"))
        if any(pp.extend_fs for pp in p.parts):
            ops.append("Conectar el destino y ampliar NTFS (diskpart «extend filesystem»)")
        if p.windows_part():
            if o.id_mode == "new" and o.fix_boot:
                ops.append(f"Reconstruir el arranque {p.firmware} con bcdboot y actualizar MountedDevices")
            if o.new_hardware:
                ops.append("Habilitar storahci, stornvme, iaStorV e iaStorAVC en el Windows clonado "
                           "(otra computadora)")
            if p.converts_to_gpt:
                ops.append("Convertir el destino a GPT/UEFI con mbr2gpt (/validate y /convert)")
            if not o.dry_run:
                ops.append("Comprobar el arranque del destino (MBR/EFI, bootmgr, BCD, hidden sectors)")
            if o.id_mode == "same":
                ops.append("Restaurar los identificadores originales y dejar el destino offline")
        if o.retrim:
            ops.append("Enviar TRIM al SSD")
        est = p.estimated_copy_bytes()
        lo, hi = estimate_minutes(est, o.verify)
        warn_html = "".join(f"<li style='color:{theme.WARN}'>{e(wm)}</li>" for wm in p.warnings)
        pending = self._pending_reasons()
        if pending:
            warn_html = (f"<li style='color:{theme.WARN}'><b>{e(PENDING_UPDATES_WARNING)}</b><br>"
                         f"Detectado: {e('; '.join(pending))}.</li>") + warn_html
        dry = (f"<p style='color:{theme.ACCENT}'><b>Modo simulación:</b> no se escribirá nada.</p>"
               if o.dry_run else "")
        win_html = ""
        if win is not None:
            info = win.win_info.display() if win.win_info else "versión no detectada"
            win_html = f"<br>Windows: <b>{e(info)}</b> en {e(win.display_name())}"
        health = self._health_html()
        level = assess_health(self.health.get(p.source.number))[0] if health else "unknown"
        health_html = (f"<p style='color:{theme.DANGER if level == 'danger' else theme.MUTED}'>{health}</p>"
                       if health else "")
        if p.converts_to_gpt:
            boot_html = ("<p><b>Arranque:</b> UEFI (el destino se convertirá a GPT con mbr2gpt). "
                         "Si la conversión falla, el SSD quedará en MBR y arrancará solo en modo "
                         "BIOS/Legacy (CSM).</p>")
        elif p.source.style == "MBR":
            boot_html = (f"<p style='color:{theme.WARN}'><b>El SSD arrancará solo en modo BIOS/Legacy "
                         "(CSM).</b> El equipo donde lo instales debe tener Legacy/CSM habilitado.</p>")
        else:
            boot_html = "<p><b>Arranque:</b> UEFI.</p>"
        extra = []
        extra.append("Sectores ilegibles: se copiarán con ceros y se listarán sus LBA al terminar."
                     if o.skip_bad_sectors else
                     "Sectores ilegibles: la copia se detendrá indicando el LBA del primero.")
        if win is not None and not o.verify:
            extra.append("Recomendado: activa «Verificar» en Opciones al clonar un Windows.")
        if o.new_hardware:
            extra.append("Windows y el software con licencia (p. ej. el del plotter) pueden pedir "
                         "reactivación por el cambio de hardware.")
        extra_html = "".join(f"<li>{e(x)}</li>" for x in extra)
        self.summary.setHtml(f"""
        <h3>Origen</h3><p>{e(p.source.title())}{win_html}</p>
        {health_html}
        <h3>Destino</h3><p>{e(p.target.title())}<br>
        <span style='color:{theme.DANGER}'><b>Todo su contenido será reemplazado.</b></span></p>
        {dry}
        {boot_html}
        <h3>Operaciones</h3><ol>{''.join(f'<li>{x}</li>' for x in ops)}</ol>
        <ul>{extra_html}</ul>
        <p>Datos a copiar: <b>{fmt_size(est)}</b> · Tiempo estimado: <b>{fmt_minutes_range(lo, hi)}</b>
        ({'incluye verificar, que vuelve a leer todo; ' if o.verify else ''}depende de la
        velocidad del HDD: al empezar se mide y el registro muestra un estimado más preciso).</p>
        {('<h3>Advertencias</h3><ul>' + warn_html + '</ul>') if warn_html else ''}
        <h3>Después de clonar</h3><ul>
        <li>Apaga el equipo, retira o desconecta el HDD y arranca desde el SSD
        (en la BIOS/UEFI elige el SSD como primer dispositivo).</li>
        <li>Si aparece la partición de recuperación desactivada, ejecuta
        <code>reagentc /enable</code> como administrador.</li></ul>
        """)
        if not keep_confirm:
            self.confirm.setChecked(False)
        # Texto corto: QCheckBox no ajusta líneas (el modelo del disco ya está en el resumen).
        self.confirm.setText(f"Entiendo que se borrará todo el contenido del Disco {p.target.number}"
                             if not o.dry_run else
                             "Entiendo que es una simulación y no se copiará nada")
