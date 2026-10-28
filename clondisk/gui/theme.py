"""Paleta y hoja de estilos de la interfaz (estilo macOS claro)."""
import os
import sys
import tempfile

from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette

# Colores del sistema de Apple (modo claro)
ACCENT = "#007AFF"
ACCENT_DARK = "#0062CC"
ACCENT_SOFT = "#E5F0FF"
SIDEBAR = "#EDEDF0"
SIDEBAR_HOVER = "#E0E0E5"
SIDEBAR_BORDER = "#D9D9DE"
SIDEBAR_TEXT = "#1D1D1F"
SIDEBAR_MUTED = "#8E8E93"
BG = "#F5F5F7"
CARD = "#FFFFFF"
BORDER = "#E2E2E7"
BORDER_STRONG = "#C7C7CC"
TEXT = "#1D1D1F"
MUTED = "#6E6E73"
DISABLED = "#AEAEB2"
DANGER = "#E5352B"
WARN = "#B25000"
OK = "#248A3D"

ROLE_COLORS = {
    "efi": "#AF52DE",
    "msr": "#8E8E93",
    "recovery": "#FF9500",
    "ntfs": "#007AFF",
    "fat": "#34C759",
    "bitlocker": "#FF3B30",
    "other": "#5AC8FA",
    "free": "#D1D1D6",
}

FONT_FAMILIES = ["SF Pro Text", "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", "Inter", "Arial"]


def fit_to_screen(widget, width: int, height: int):
    """Redimensiona a ``width``x``height`` sin pasarse del área útil de la pantalla.

    El margen deja sitio para la barra de título y el borde de la ventana, que no
    cuentan en ``resize``; sin él, en pantallas de 768 px los botones quedan bajo la
    barra de tareas.
    """
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        width = min(width, avail.width() - 20)
        height = min(height, avail.height() - 50)
    widget.resize(width, height)


def part_color(role: str, fs: str) -> QColor:
    if fs == "BitLocker":
        return QColor(ROLE_COLORS["bitlocker"])
    if role in ("efi", "msr", "recovery"):
        return QColor(ROLE_COLORS[role])
    if fs == "NTFS":
        return QColor(ROLE_COLORS["ntfs"])
    if fs.startswith("FAT") or fs == "exFAT":
        return QColor(ROLE_COLORS["fat"])
    return QColor(ROLE_COLORS["other"])


# ---------------------------------------------------------------- iconos SVG
_CHECK = ('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">'
          '<path d="M4.5 9.3l3 3 6-6.6" fill="none" stroke="{c}" stroke-width="2.2" '
          'stroke-linecap="round" stroke-linejoin="round"/></svg>')
_DOT = ('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">'
        '<circle cx="9" cy="9" r="3.6" fill="{c}"/></svg>')
_CHEVRON = ('<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
            '<path d="M3 4.5l3 3 3-3" fill="none" stroke="{c}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>')
_ARROW_UP = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
             '<path d="M2.5 6.2l2.5-2.5 2.5 2.5" fill="none" stroke="{c}" stroke-width="1.5" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')
_ARROW_DOWN = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">'
               '<path d="M2.5 3.8l2.5 2.5 2.5-2.5" fill="none" stroke="{c}" stroke-width="1.5" '
               'stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _write_icons() -> dict:
    """Guarda los iconos en una carpeta temporal; QSS solo acepta rutas de archivo."""
    folder = os.path.join(tempfile.gettempdir(), "clondisk-theme")
    os.makedirs(folder, exist_ok=True)
    icons = {
        "check": _CHECK.format(c="#FFFFFF"),
        "check_disabled": _CHECK.format(c="#F2F2F7"),
        "dot": _DOT.format(c="#FFFFFF"),
        "dot_disabled": _DOT.format(c="#F2F2F7"),
        "chevron": _CHEVRON.format(c=MUTED),
        "up": _ARROW_UP.format(c=MUTED),
        "down": _ARROW_DOWN.format(c=MUTED),
    }
    paths = {}
    for name, svg in icons.items():
        path = os.path.join(folder, f"{name}.svg")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
        except OSError:
            pass
        paths[name] = path.replace("\\", "/")
    return paths


def _palette() -> QPalette:
    """Paleta clara fija: así no se heredan el modo oscuro ni el color de acento de Windows."""
    pal = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: CARD,
        QPalette.AlternateBase: "#FAFAFC", QPalette.Text: TEXT, QPalette.Button: CARD,
        QPalette.ButtonText: TEXT, QPalette.BrightText: "#FFFFFF", QPalette.Highlight: ACCENT,
        QPalette.HighlightedText: "#FFFFFF", QPalette.ToolTipBase: "#FFFFFF",
        QPalette.ToolTipText: TEXT, QPalette.PlaceholderText: DISABLED, QPalette.Link: ACCENT,
        QPalette.Light: "#FFFFFF", QPalette.Midlight: "#F2F2F7", QPalette.Mid: BORDER_STRONG,
        QPalette.Dark: "#8E8E93", QPalette.Shadow: "#00000040",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(DISABLED))
    pal.setColor(QPalette.Disabled, QPalette.Highlight, QColor(BORDER_STRONG))
    return pal


def build_stylesheet(ic: dict) -> str:
    return f"""
QWidget {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: white; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; }}

/* ---------- barra lateral ---------- */
#Sidebar {{ background: {SIDEBAR}; border-right: 1px solid {SIDEBAR_BORDER}; }}
#Sidebar QLabel {{ color: {SIDEBAR_TEXT}; }}
#AppTitle {{ font-size: 17pt; font-weight: 700; color: {SIDEBAR_TEXT}; }}
#Sidebar QLabel#AppSubtitle, #AppSubtitle {{ color: {SIDEBAR_MUTED}; font-size: 9pt; }}
#SideSection {{ color: {SIDEBAR_MUTED}; font-size: 8.5pt; font-weight: 600; padding-left: 10px; }}
#SideButton {{
    text-align: left; padding: 8px 12px; border: none; border-radius: 8px;
    color: {SIDEBAR_TEXT}; background: transparent; font-size: 10.5pt;
}}
#SideButton:hover {{ background: {SIDEBAR_HOVER}; }}
#SideButton:checked {{ background: {ACCENT}; color: white; font-weight: 600; }}

/* ---------- títulos y tarjetas ---------- */
#PageTitle {{ font-size: 18pt; font-weight: 700; }}
#PageSubtitle {{ color: {MUTED}; }}
#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
#Card[selected="true"] {{ border: 2px solid {ACCENT}; background: #F7FAFF; }}
#Card[disabled="true"] {{ background: #FAFAFB; }}
#CardTitle {{ font-weight: 600; font-size: 10.5pt; }}
#Chip {{ border-radius: 8px; padding: 1px 8px; font-size: 8pt; font-weight: 600; }}
#LinkButton {{ border: none; color: {ACCENT}; background: transparent; font-weight: 600; padding: 2px 4px; }}
#LinkButton:hover {{ color: {ACCENT_DARK}; }}
#LinkButton:disabled {{ color: {DISABLED}; }}

/* ---------- botones ---------- */
QPushButton {{
    background: white; border: 1px solid {BORDER_STRONG}; border-radius: 8px; padding: 6px 16px;
    min-height: 20px;
}}
QPushButton:hover {{ background: #FAFAFC; }}
QPushButton:pressed {{ background: #EDEDF0; }}
QPushButton:disabled {{ color: {DISABLED}; background: #F5F5F7; border-color: {BORDER}; }}
QPushButton#Primary {{ background: {ACCENT}; color: white; border: 1px solid {ACCENT}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #1A88FF; border-color: #1A88FF; }}
QPushButton#Primary:pressed {{ background: {ACCENT_DARK}; border-color: {ACCENT_DARK}; }}
QPushButton#Primary:disabled {{ background: #A8CFFF; border-color: #A8CFFF; color: white; }}
QPushButton#Danger {{ background: {DANGER}; color: white; border: 1px solid {DANGER}; font-weight: 600; }}
QPushButton#Danger:hover {{ background: #F0453B; border-color: #F0453B; }}
QPushButton#Danger:disabled {{ background: #F4B4AF; border-color: #F4B4AF; color: white; }}

/* ---------- casillas y opciones ---------- */
QCheckBox, QRadioButton {{ spacing: 9px; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {DISABLED}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid {BORDER_STRONG}; background: white;
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({ic['check']}); }}
QCheckBox::indicator:checked:hover {{ background: #1A88FF; border-color: #1A88FF; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url({ic['dot']}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: #F2F2F7; border-color: {BORDER}; }}
QCheckBox::indicator:checked:disabled {{ background: {BORDER_STRONG}; border-color: {BORDER_STRONG}; image: url({ic['check_disabled']}); }}
QRadioButton::indicator:checked:disabled {{ background: {BORDER_STRONG}; border-color: {BORDER_STRONG}; image: url({ic['dot_disabled']}); }}

/* ---------- campos ---------- */
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: white; border: 1px solid {BORDER_STRONG}; border-radius: 7px; padding: 4px 8px;
    min-height: 20px; selection-background-color: {ACCENT};
}}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border: 2px solid #7DB8FF; padding: 3px 7px; }}
QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled {{
    background: #F5F5F7; color: {DISABLED}; border-color: {BORDER};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url({ic['chevron']}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: white; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px;
    selection-background-color: {ACCENT}; selection-color: white; outline: none;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
    border: none; width: 18px; background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({ic['up']}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({ic['down']}); width: 10px; height: 10px; }}

QSlider::groove:horizontal {{ height: 4px; background: {SIDEBAR_HOVER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: white; border: 1px solid {BORDER_STRONG}; width: 18px; height: 18px;
    margin: -8px 0; border-radius: 10px;
}}
QSlider::handle:horizontal:hover {{ border-color: {ACCENT}; }}
QSlider::sub-page:horizontal:disabled {{ background: {BORDER_STRONG}; }}
QSlider::handle:horizontal:disabled {{ background: #F5F5F7; border-color: {BORDER}; }}

/* ---------- listas de pasos (asistente y progreso) ---------- */
#StepList {{
    background: {SIDEBAR}; border: none; border-right: 1px solid {SIDEBAR_BORDER};
    color: {SIDEBAR_MUTED}; padding: 14px 10px; font-size: 10.5pt; outline: none;
}}
#StepList::item {{ padding: 9px 12px; border-radius: 8px; margin: 1px 0; }}
#StepList::item:selected {{ background: white; color: {ACCENT}; font-weight: 600; border: 1px solid {SIDEBAR_BORDER}; }}

/* ---------- avisos ---------- */
#Banner {{ border-radius: 10px; padding: 9px 12px; }}
#Banner[kind="error"] {{ background: #FFF0EF; border: 1px solid #FFCFCB; color: #C4271E; }}
#Banner[kind="warn"] {{ background: #FFF6E8; border: 1px solid #FFDDA8; color: #8A4B00; }}
#Banner[kind="info"] {{ background: {ACCENT_SOFT}; border: 1px solid #BBD8FF; color: #0A4A99; }}
#Banner[kind="ok"] {{ background: #EAF8EE; border: 1px solid #B5E6C2; color: {OK}; }}

/* ---------- tablas, texto y progreso ---------- */
QTableWidget {{
    background: white; border: 1px solid {BORDER}; border-radius: 10px; gridline-color: transparent;
    alternate-background-color: #FAFAFC; selection-background-color: {ACCENT_SOFT}; selection-color: {TEXT};
}}
QTableWidget::item {{ padding: 4px 6px; border-bottom: 1px solid #F0F0F3; }}
QHeaderView::section {{
    background: white; border: none; border-bottom: 1px solid {BORDER}; padding: 7px 6px;
    font-weight: 600; color: {MUTED};
}}
QTableCornerButton::section {{ background: white; border: none; }}
QPlainTextEdit, QTextEdit, QTextBrowser {{
    background: white; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px;
    selection-background-color: {ACCENT};
}}
QProgressBar {{
    border: none; border-radius: 6px; background: #E5E5EA; height: 12px; max-height: 12px;
    text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}
QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 12px; margin-top: 22px; background: white;
    padding: 12px 10px 10px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left; left: 4px; padding: 0 4px 4px 4px;
    font-weight: 600; color: {MUTED};
}}
QScrollArea {{ border: none; background: transparent; }}
#ScrollBody {{ background: transparent; }}
QStatusBar {{ background: {BG}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}

/* ---------- barras de desplazamiento finas ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C7C7CC; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #A1A1A6; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #C7C7CC; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: #A1A1A6; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def apply(app):
    """Aplica estilo, paleta, tipografía y hoja de estilos a la aplicación."""
    app.setStyle("Fusion")
    app.setPalette(_palette())
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSizeF(10)
    font.setHintingPreference(QFont.PreferNoHinting if sys.platform == "darwin" else QFont.PreferVerticalHinting)
    app.setFont(font)
    app.setStyleSheet(build_stylesheet(_write_icons()))
