"""Abstracción de dispositivos de bloque (disco físico, instantánea VSS, imagen)."""
from __future__ import annotations

import os
import threading


class DeviceError(IOError):
    pass


class Device:
    """Interfaz mínima. Las lecturas/escrituras deben ir alineadas a sector;
    :meth:`read_any` permite leer en cualquier posición."""

    sector_size: int = 512
    size: int = 0
    path: str = ""

    def read(self, offset: int, length: int) -> bytes:  # pragma: no cover - interfaz
        raise NotImplementedError

    def write(self, offset: int, data: bytes) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    def read_any(self, offset: int, length: int) -> bytes:
        ss = self.sector_size
        start = (offset // ss) * ss
        end = -(-(offset + length) // ss) * ss
        data = self.read(start, end - start)
        return data[offset - start: offset - start + length]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FileDevice(Device):
    """Dispositivo respaldado por un archivo de imagen (.img / .bin).

    Se usa en el *modo laboratorio* y en las pruebas automáticas.
    """

    def __init__(self, path: str, writable: bool = False, sector_size: int = 512):
        self.path = path
        self.sector_size = sector_size
        self._f = open(path, "r+b" if writable else "rb", buffering=0)
        self._lock = threading.Lock()
        self.size = os.path.getsize(path)
        self.writable = writable

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset + length > self.size:
            raise DeviceError(f"Lectura fuera de rango en {self.path}: {offset}+{length} > {self.size}")
        with self._lock:
            self._f.seek(offset)
            chunks = []
            remaining = length
            while remaining:
                b = self._f.read(remaining)
                if not b:
                    raise DeviceError(f"Fin de archivo inesperado en {self.path}")
                chunks.append(b)
                remaining -= len(b)
            return b"".join(chunks)

    def write(self, offset: int, data: bytes) -> None:
        if not self.writable:
            raise DeviceError(f"{self.path} está abierto en solo lectura")
        if offset < 0 or offset + len(data) > self.size:
            raise DeviceError(f"Escritura fuera de rango en {self.path}")
        with self._lock:
            self._f.seek(offset)
            view = memoryview(data)
            while view:
                n = self._f.write(view)
                view = view[n:]

    def flush(self) -> None:
        with self._lock:
            self._f.flush()
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


class WindowView(Device):
    """Vista de una región de otro dispositivo (p. ej. una partición)."""

    def __init__(self, parent: Device, base: int, size: int):
        self.parent = parent
        self.base = base
        self.size = size
        self.sector_size = parent.sector_size
        self.path = f"{parent.path}@{base}"

    def read(self, offset: int, length: int) -> bytes:
        if offset + length > self.size:
            raise DeviceError("Lectura fuera de la partición")
        return self.parent.read(self.base + offset, length)

    def write(self, offset: int, data: bytes) -> None:
        if offset + len(data) > self.size:
            raise DeviceError("Escritura fuera de la partición")
        self.parent.write(self.base + offset, data)
