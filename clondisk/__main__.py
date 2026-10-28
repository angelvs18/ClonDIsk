"""Punto de entrada: ``python -m clondisk``.

Opciones:
  --images A.img B.img ...   Modo laboratorio con archivos de imagen.
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="clondisk", description="Clonación de discos HDD → SSD")
    ap.add_argument("--images", nargs="+", metavar="IMG",
                    help="usar archivos de imagen como discos (modo laboratorio, sin riesgo)")
    args = ap.parse_args(argv)

    if args.images:
        from .platforms.images import ImagePlatform
        platform = ImagePlatform(args.images)
    elif os.name == "nt":
        from .platforms import windows as win
        if not win.is_admin():
            if win.relaunch_as_admin(argv):
                return 0
            _fatal("ClonDisk necesita permisos de administrador para acceder a los discos.")
            return 1
        platform = win.WindowsPlatform()
    else:
        _fatal("Fuera de Windows solo está disponible el modo laboratorio:\n"
               "  python -m clondisk --images origen.img destino.img")
        return 1

    from PySide6.QtWidgets import QApplication
    from .gui import theme
    from .gui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("ClonDisk")
    theme.apply(app)
    win_ = MainWindow(platform)
    win_.show()
    win_.refresh()
    return app.exec()


def _fatal(msg: str):
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        _app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841
        QMessageBox.critical(None, "ClonDisk", msg)
    except Exception:
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
