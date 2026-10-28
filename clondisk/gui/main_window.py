"""Ventana principal: lista de discos estilo Macrium Reflect."""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QScrollArea, QStackedWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..core.model import Disk
from . import theme
from .diskmap import DiskCard
from .progress import ProgressDialog, run_in_thread
from .wizard import CloneWizard

ABOUT_HTML = f"""
<h2>ClonDisk {__version__}</h2>
<p>Clonación de discos HDD → SSD con soporte para GPT/MBR, particiones EFI, MSR y de
recuperación, copia inteligente de NTFS, instantáneas VSS y reparación del arranque.</p>
<h3>Cómo funciona</h3>
<ol>
<li>Lee la tabla de particiones directamente de los sectores del disco.</li>
<li>Planifica el destino: alinea a 1&nbsp;MiB, conserva EFI/MSR/recuperación y ajusta la
partición de Windows al tamaño del SSD.</li>
<li>Crea instantáneas VSS para copiar Windows en uso de forma consistente.</li>
<li>Copia solo los clústeres usados de NTFS (según <code>$Bitmap</code>) y el resto sector a sector.</li>
<li>Escribe la tabla de particiones al final, amplía NTFS y reconstruye el arranque
(<code>bcdboot</code>) y <code>MountedDevices</code>.</li>
</ol>
<h3>Limitaciones</h3>
<ul>
<li>No admite discos dinámicos, Espacios de almacenamiento, particiones lógicas MBR ni BitLocker activo.</li>
<li>Origen y destino deben tener el mismo tamaño de sector lógico (512 o 4096 B).</li>
<li>Para clonar a un disco más pequeño, primero se reduce la partición de origen con las herramientas de Windows.</li>
</ul>
<p style="color:{theme.DANGER}"><b>Haz siempre un respaldo de tus datos importantes antes de clonar.</b></p>
"""


class MainWindow(QMainWindow):
    def __init__(self, platform):
        super().__init__()
        self.platform = platform
        self.disks: List[Disk] = []
        self.setWindowTitle("ClonDisk – Clonación de discos")
        theme.fit_to_screen(self, 1180, 760)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- barra lateral ---------------------------------------------------
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(230)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(4)
        t = QLabel("ClonDisk")
        t.setObjectName("AppTitle")
        st = QLabel("HDD → SSD sin complicaciones")
        st.setObjectName("AppSubtitle")
        sl.addWidget(t)
        sl.addWidget(st)
        sl.addSpacing(18)
        self.nav = QButtonGroup(self)
        self.btn_disks = self._side_button("Discos", True)
        self.btn_about = self._side_button("Acerca de", True)
        self.nav.addButton(self.btn_disks, 0)
        self.nav.addButton(self.btn_about, 1)
        self.btn_disks.setChecked(True)
        sl.addWidget(self.btn_disks)
        sl.addWidget(self.btn_about)
        sl.addSpacing(12)
        lbl = QLabel("TAREAS")
        lbl.setObjectName("SideSection")
        sl.addWidget(lbl)
        self.btn_clone = self._side_button("Clonar un disco…")
        self.btn_clone.clicked.connect(lambda: self.open_wizard(None))
        sl.addWidget(self.btn_clone)
        self.btn_refresh = self._side_button("Actualizar discos")
        self.btn_refresh.clicked.connect(self.refresh)
        sl.addWidget(self.btn_refresh)
        sl.addStretch(1)
        mode = QLabel(f"Plataforma: {platform.name}")
        mode.setObjectName("AppSubtitle")
        mode.setWordWrap(True)
        sl.addWidget(mode)
        root.addWidget(side)

        # --- contenido ---------------------------------------------------------
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_disks_page())
        about = QTextBrowser()
        about.setHtml(ABOUT_HTML)
        about.setOpenExternalLinks(True)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(24, 20, 24, 20)
        wl.addWidget(about)
        self.pages.addWidget(wrap)
        self.nav.idClicked.connect(self.pages.setCurrentIndex)
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        if platform.is_lab:
            self.statusBar().showMessage("Modo laboratorio: se trabaja con archivos de imagen, no con discos reales")
        else:
            self.statusBar().showMessage("Listo")

    def _side_button(self, text: str, checkable: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("SideButton")
        b.setCheckable(checkable)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _build_disks_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 20, 24, 12)
        head = QHBoxLayout()
        tl = QVBoxLayout()
        t = QLabel("Discos detectados")
        t.setObjectName("PageTitle")
        s = QLabel("Elige «Clonar este disco…» en el disco de origen (tu HDD actual).")
        s.setObjectName("PageSubtitle")
        tl.addWidget(t)
        tl.addWidget(s)
        head.addLayout(tl, 1)
        b = QPushButton("Clonar un disco…")
        b.setObjectName("Primary")
        b.clicked.connect(lambda: self.open_wizard(None))
        head.addWidget(b, 0, Qt.AlignBottom)
        lay.addLayout(head)
        lay.addSpacing(8)

        self.list_body = QWidget()
        self.list_body.setObjectName("ScrollBody")
        self.list_layout = QVBoxLayout(self.list_body)
        self.list_layout.setSpacing(12)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(self.list_body)
        lay.addWidget(sa, 1)

        legend = QHBoxLayout()
        for name, key in (("NTFS", "ntfs"), ("FAT32", "fat"), ("Sistema EFI", "efi"),
                          ("Reservada MSR", "msr"), ("Recuperación", "recovery"),
                          ("BitLocker", "bitlocker"), ("Sin asignar", "free")):
            sw = QLabel()
            sw.setFixedSize(12, 12)
            sw.setStyleSheet(f"background:{theme.ROLE_COLORS[key]}; border-radius:2px;")
            legend.addWidget(sw)
            lab = QLabel(name)
            lab.setStyleSheet(f"color:{theme.MUTED}; font-size:9pt; margin-right:10px;")
            legend.addWidget(lab)
        legend.addStretch(1)
        lay.addLayout(legend)
        return page

    # ------------------------------------------------------------------
    def refresh(self):
        self.statusBar().showMessage("Leyendo discos…")
        self.btn_refresh.setEnabled(False)
        self.setCursor(Qt.WaitCursor)
        run_in_thread(self, self.platform.list_disks, on_done=self._on_disks)

    def _on_disks(self, disks, err):
        self.unsetCursor()
        self.btn_refresh.setEnabled(True)
        while self.list_layout.count():
            it = self.list_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if err:
            msg = QLabel(f"No se pudieron leer los discos:\n{err}")
            msg.setStyleSheet(f"color:{theme.DANGER};")
            msg.setWordWrap(True)
            self.list_layout.addWidget(msg)
            self.statusBar().showMessage("Error al leer los discos")
            self.list_layout.addStretch(1)
            return
        self.disks = disks
        for d in disks:
            card = DiskCard(d, show_clone=True)
            card.clone_requested.connect(self.open_wizard)
            self.list_layout.addWidget(card)
        self.list_layout.addStretch(1)
        self.statusBar().showMessage(f"{len(disks)} disco(s) detectado(s)")

    def open_wizard(self, source: Optional[Disk]):
        if not self.disks:
            QMessageBox.information(self, "ClonDisk", "Aún no hay discos cargados.")
            return
        if len(self.disks) < 2:
            QMessageBox.information(self, "ClonDisk",
                                    "Solo hay un disco conectado. Conecta el SSD (por SATA, M.2 "
                                    "o un adaptador USB) y pulsa «Actualizar discos».")
            return
        wiz = CloneWizard(self.platform, self.disks, source, self)
        if wiz.exec() and wiz.plan and wiz.plan.ok:
            dlg = ProgressDialog(self.platform, wiz.plan, self)
            dlg.start()
            dlg.exec()
            self.refresh()
