"""Mapa gráfico de un disco (bloques de particiones) y tarjetas de disco."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QRadioButton,
                               QSizePolicy, QStyle, QToolTip, QVBoxLayout, QWidget)

from ..core.model import MiB, Disk, fmt_size
from ..core.planner import ClonePlan
from . import theme


@dataclass
class Segment:
    title: str
    subtitle: str
    size: int
    used: Optional[int]
    color: QColor
    free: bool = False
    highlight: bool = False
    tooltip: str = ""


def disk_segments(disk: Disk) -> List[Segment]:
    ss = disk.sector_size
    segs: List[Segment] = []
    pos = 0
    min_gap = 16 * MiB // ss
    for p in sorted(disk.partitions, key=lambda p: p.start_lba):
        if p.start_lba - pos > min_gap:
            segs.append(_free((p.start_lba - pos) * ss))
        segs.append(Segment(
            title=p.display_name(), subtitle=p.fs,
            size=p.size_bytes(ss), used=p.used_bytes, color=theme.part_color(p.role, p.fs),
            tooltip=_tip(p.display_name(), p.type_name, p.fs, p.size_bytes(ss), p.used_bytes,
                         p.offset_bytes(ss))))
        pos = max(pos, p.end_lba)
    tail = disk.total_sectors - pos
    if tail > min_gap or not disk.partitions:
        segs.append(_free(tail * ss))
    return segs


def plan_segments(plan: ClonePlan) -> List[Segment]:
    ss = plan.sector_size
    total = plan.target.size_bytes // ss
    segs: List[Segment] = []
    pos = 0
    min_gap = 16 * MiB // ss
    for pp in plan.parts:
        p = pp.src
        if pp.new_start_lba - pos > min_gap:
            segs.append(_free((pp.new_start_lba - pos) * ss))
        size = pp.new_sectors * ss
        delta = ""
        if pp.new_sectors != p.sectors:
            delta = f"  (antes {fmt_size(p.size_bytes(ss))})"
        segs.append(Segment(
            title=p.display_name(), subtitle=p.fs, size=size, used=p.used_bytes,
            color=theme.part_color(p.role, p.fs), highlight=pp.resizable,
            tooltip=_tip(p.display_name(), p.type_name, p.fs, size, p.used_bytes,
                         pp.new_start_lba * ss) + delta))
        pos = pp.new_start_lba + pp.new_sectors
    if total - pos > min_gap:
        segs.append(_free((total - pos) * ss))
    return segs


def _free(size: int) -> Segment:
    return Segment("Sin asignar", "", size, None, QColor(theme.ROLE_COLORS["free"]), free=True,
                   tooltip=f"Espacio sin asignar: {fmt_size(size)}")


def _tip(name, type_name, fs, size, used, offset) -> str:
    used_s = f"\nEn uso: {fmt_size(used)}" if used is not None else ""
    return (f"{name}\nTipo: {type_name}\nSistema de archivos: {fs}\nTamaño: {fmt_size(size)}"
            f"{used_s}\nInicio: {fmt_size(offset)} ({offset // 512} × 512 B)")


class DiskMapWidget(QWidget):
    """Barra horizontal con un bloque por partición, al estilo Macrium/Disk Management."""

    MIN_W = 78

    def __init__(self, segments: List[Segment] | None = None, parent=None):
        super().__init__(parent)
        self.segments: List[Segment] = segments or []
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._rects: List[QRectF] = []

    def sizeHint(self):
        return QSize(600, 76)

    def set_segments(self, segments: List[Segment]):
        self.segments = segments
        self.update()

    def _layout(self) -> List[QRectF]:
        w = self.width() - 2
        n = len(self.segments)
        if not n:
            return []
        gap = 4
        avail = w - gap * (n - 1)
        total = sum(max(s.size, 1) for s in self.segments)
        widths = [self.MIN_W] * n
        rest = avail - self.MIN_W * n
        if rest > 0:
            for i, s in enumerate(self.segments):
                widths[i] += rest * max(s.size, 1) / total
        else:
            widths = [avail / n] * n
        rects, x = [], 1.0
        for wd in widths:
            rects.append(QRectF(x, 1, wd, self.height() - 2))
            x += wd + gap
        return rects

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self._rects = self._layout()
        f_title = QFont(self.font())
        f_title.setBold(True)
        f_small = QFont(self.font())
        f_small.setPointSizeF(max(7.5, self.font().pointSizeF() - 1.5))
        for seg, r in zip(self.segments, self._rects):
            path = QPainterPath()
            path.addRoundedRect(r, 8, 8)
            if seg.free:
                p.fillPath(path, QColor("#F5F5F7"))
                p.setPen(QPen(QColor(theme.BORDER_STRONG), 1, Qt.DashLine))
                p.drawPath(path)
            else:
                p.fillPath(path, QColor("white"))
                p.setPen(QPen(QColor(theme.ACCENT if seg.highlight else theme.BORDER),
                              2 if seg.highlight else 1, Qt.DashLine if seg.highlight else Qt.SolidLine))
                p.drawPath(path)
                # franja de color superior
                p.save()
                p.setClipPath(path)
                p.fillRect(QRectF(r.x(), r.y(), r.width(), 5), seg.color)
                p.restore()
                # barra de uso
                if seg.used is not None and seg.size:
                    frac = max(0.0, min(1.0, seg.used / seg.size))
                    bar = QRectF(r.x() + 6, r.bottom() - 12, r.width() - 12, 6)
                    track = QPainterPath()
                    track.addRoundedRect(bar, 3, 3)
                    p.fillPath(track, QColor("#E5E5EA"))
                    fill = QPainterPath()
                    fill.addRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * frac, bar.height()), 3, 3)
                    p.fillPath(fill, seg.color)
            # textos
            inner = r.adjusted(6, 10, -6, -14)
            p.setPen(QColor(theme.TEXT if not seg.free else theme.MUTED))
            p.setFont(f_title)
            fm = QFontMetrics(f_title)
            p.drawText(inner, Qt.AlignLeft | Qt.AlignTop,
                       fm.elidedText(seg.title, Qt.ElideRight, int(inner.width())))
            p.setFont(f_small)
            fm2 = QFontMetrics(f_small)
            line2 = seg.subtitle
            if seg.used is not None:
                line3 = f"{fmt_size(seg.used)} / {fmt_size(seg.size)}"
            else:
                line3 = fmt_size(seg.size)
            y = inner.y() + fm.height() + 1
            p.setPen(QColor(theme.MUTED))
            if line2:
                p.drawText(QRectF(inner.x(), y, inner.width(), fm2.height()), Qt.AlignLeft,
                           fm2.elidedText(line2, Qt.ElideRight, int(inner.width())))
                y += fm2.height()
            p.drawText(QRectF(inner.x(), y, inner.width(), fm2.height()), Qt.AlignLeft,
                       fm2.elidedText(line3, Qt.ElideRight, int(inner.width())))
        p.end()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        for seg, r in zip(self.segments, self._rects):
            if r.contains(pos):
                QToolTip.showText(ev.globalPosition().toPoint(), seg.tooltip, self)
                return
        QToolTip.hideText()


def chip(text: str, bg: str, fg: str = "white") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Chip")
    lab.setStyleSheet(f"background:{bg}; color:{fg};")
    return lab


class DiskCard(QFrame):
    """Tarjeta con encabezado del disco y su mapa de particiones."""

    clicked = Signal(object)
    clone_requested = Signal(object)

    def __init__(self, disk: Disk, selectable: bool = False, show_clone: bool = False,
                 disabled_reason: str = "", parent=None):
        super().__init__(parent)
        self.disk = disk
        self.selectable = selectable
        self.disabled_reason = disabled_reason
        self.setObjectName("Card")
        self.setProperty("disabled", "true" if disabled_reason else "false")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        if selectable:
            self.radio = QRadioButton()
            self.radio.setEnabled(not disabled_reason)
            self.radio.toggled.connect(self._on_toggled)
            head.addWidget(self.radio)
        else:
            self.radio = None
        icon = QLabel()
        sp = QStyle.SP_DriveHDIcon
        icon.setPixmap(self.style().standardIcon(sp).pixmap(22, 22))
        head.addWidget(icon)
        title = QLabel(disk.title())
        title.setObjectName("CardTitle")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(title, 1)
        if disk.is_running_system:
            head.addWidget(chip("SISTEMA", "#636366"))
        if disk.media == "SSD":
            head.addWidget(chip("SSD", "#34C759"))
        elif disk.media == "HDD":
            head.addWidget(chip("HDD", "#A2845E"))
        if disk.is_offline:
            head.addWidget(chip("OFFLINE", "#FF9500"))
        if show_clone:
            btn = QPushButton("Clonar este disco…")
            btn.setObjectName("LinkButton")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setEnabled(disk.style in ("GPT", "MBR"))
            btn.clicked.connect(lambda: self.clone_requested.emit(self.disk))
            head.addWidget(btn)
        lay.addLayout(head)

        self.map = DiskMapWidget(disk_segments(disk))
        lay.addWidget(self.map)

        notes = list(disk.problems)
        if disabled_reason:
            notes.insert(0, disabled_reason)
        if notes:
            n = QLabel("\n".join(notes))
            n.setWordWrap(True)
            n.setStyleSheet(f"color:{theme.DANGER if disabled_reason else theme.WARN}; font-size:9pt;")
            lay.addWidget(n)

    def set_selected(self, sel: bool):
        self.setProperty("selected", "true" if sel else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        if self.radio and self.radio.isChecked() != sel:
            self.radio.blockSignals(True)
            self.radio.setChecked(sel)
            self.radio.blockSignals(False)

    def _on_toggled(self, checked: bool):
        if checked:
            self.clicked.emit(self.disk)

    def mousePressEvent(self, ev):
        if self.selectable and not self.disabled_reason:
            self.clicked.emit(self.disk)
        super().mousePressEvent(ev)
