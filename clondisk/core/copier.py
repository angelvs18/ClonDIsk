"""Copia por rangos con lectura y escritura en paralelo (pipeline).

Un hilo lee del origen mientras el hilo principal escribe en el destino, de
modo que un HDD lento y un SSD rápido trabajan a la vez.

Si una lectura falla, el bloque se divide a la mitad y se relee cada parte
hasta llegar a un sector (con :data:`SECTOR_RETRIES` intentos por sector).
Un sector que sigue sin leerse aborta la copia o, con ``skip_bad_sectors``,
se escribe con ceros y se registra su LBA.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, Iterable, List, Optional, Tuple

from .device import Device
from .model import MiB

Range = Tuple[int, int]


class CancelledError(Exception):
    pass


class VerifyError(Exception):
    pass


class BadSectorError(IOError):
    def __init__(self, lba: int, cause: BaseException):
        super().__init__(f"Sector ilegible en el disco de origen: LBA {lba} ({cause}). "
                         "Activa «Saltar sectores ilegibles» para copiarlo con ceros, o "
                         "revisa el disco/cable/adaptador.")
        self.lba = lba


SECTOR_RETRIES = 3


def split_ranges(ranges: Iterable[Range], chunk: int) -> List[Range]:
    out = []
    for off, ln in ranges:
        pos = 0
        while pos < ln:
            n = min(chunk, ln - pos)
            out.append((off + pos, n))
            pos += n
    return out


class Copier:
    def __init__(self, cancel: threading.Event,
                 on_progress: Optional[Callable[[int], None]] = None,
                 chunk_size: int = 8 * MiB, queue_depth: int = 4,
                 skip_bad_sectors: bool = False,
                 on_bad_sector: Optional[Callable[[int], None]] = None):
        self.cancel = cancel
        self.on_progress = on_progress or (lambda n: None)
        self.chunk = chunk_size
        self.depth = queue_depth
        self.skip_bad = skip_bad_sectors
        self.on_bad_sector = on_bad_sector or (lambda lba: None)
        self.bad_sectors: List[int] = []   # LBA (del disco de origen) escritos con ceros

    def _read_robust(self, src: Device, off: int, ln: int, lba_of: Callable[[int], int]) -> bytes:
        """Lee ``ln`` bytes en ``off``; ante un error divide el bloque a la mitad."""
        ss = src.sector_size
        if ln <= ss:
            return self._read_sector(src, off, ln, lba_of)
        try:
            return src.read(off, ln)
        except OSError:
            half = max(ss, (ln // 2) // ss * ss)
            self._check()
            return (self._read_robust(src, off, half, lba_of)
                    + self._read_robust(src, off + half, ln - half, lba_of))

    def _read_sector(self, src: Device, off: int, ln: int, lba_of) -> bytes:
        """Un sector: hasta :data:`SECTOR_RETRIES` intentos en total."""
        err: Optional[OSError] = None
        for _ in range(SECTOR_RETRIES):
            self._check()
            try:
                return src.read(off, ln)
            except OSError as e:
                err = e
        lba = lba_of(off)
        if not self.skip_bad:
            raise BadSectorError(lba, err)
        self.bad_sectors.append(lba)
        self.on_bad_sector(lba)
        return b"\0" * ln

    def _check(self):
        if self.cancel.is_set():
            raise CancelledError("Operación cancelada por el usuario")

    def copy(self, src: Device, src_base: int, dst: Device, dst_base: int,
             ranges: List[Range], lba_base: Optional[int] = None) -> int:
        """Copia ``ranges`` (relativos a la partición) de src a dst. ``lba_base`` es el
        LBA del disco de origen que corresponde al offset 0 de los rangos (para los
        mensajes de sectores ilegibles); por defecto ``src_base / sector``."""
        ss = src.sector_size
        base_lba = src_base // ss if lba_base is None else lba_base

        def lba_of(abs_off: int) -> int:
            return base_lba + (abs_off - src_base) // ss

        chunks = split_ranges(ranges, self.chunk)
        q: "queue.Queue" = queue.Queue(maxsize=self.depth)
        stop = threading.Event()
        error: List[BaseException] = []

        def reader():
            try:
                for off, ln in chunks:
                    if stop.is_set() or self.cancel.is_set():
                        break
                    data = self._read_robust(src, src_base + off, ln, lba_of)
                    while not stop.is_set():
                        try:
                            q.put((off, data), timeout=0.2)
                            break
                        except queue.Full:
                            continue
            except BaseException as e:  # se propaga al hilo principal
                error.append(e)
            finally:
                while not stop.is_set():
                    try:
                        q.put(None, timeout=0.2)
                        break
                    except queue.Full:
                        continue

        t = threading.Thread(target=reader, name="clondisk-reader", daemon=True)
        t.start()
        written = 0
        try:
            while True:
                item = q.get()
                if item is None:
                    break
                self._check()
                off, data = item
                dst.write(dst_base + off, data)
                written += len(data)
                self.on_progress(len(data))
            if error:
                raise error[0]
            self._check()
        finally:
            stop.set()
            t.join(timeout=10)
        return written

    def verify(self, src: Device, src_base: int, dst: Device, dst_base: int,
               ranges: List[Range], lba_base: Optional[int] = None) -> int:
        ss = src.sector_size
        base_lba = src_base // ss if lba_base is None else lba_base
        bad = set(self.bad_sectors)
        checked = 0
        for off, ln in split_ranges(ranges, self.chunk):
            self._check()
            b = dst.read(dst_base + off, ln)
            try:
                a = src.read(src_base + off, ln)
            except OSError:
                # Bloque con sectores ilegibles: comparar sector a sector, omitiendo
                # los que ya se copiaron con ceros.
                parts = []
                for i in range(0, ln, ss):
                    lba = base_lba + (off + i) // ss
                    if lba in bad:
                        parts.append(b[i:i + ss])
                        continue
                    try:
                        parts.append(src.read(src_base + off + i, min(ss, ln - i)))
                    except OSError as e:
                        raise VerifyError(f"No se pudo releer el LBA {lba} del origen: {e}")
                a = b"".join(parts)
            if a != b:
                # localizar el primer sector distinto para el registro
                ss = dst.sector_size
                for i in range(0, ln, ss):
                    if a[i:i + ss] != b[i:i + ss]:
                        raise VerifyError(
                            f"Diferencia en offset {dst_base + off + i} del destino")
                raise VerifyError("Los datos verificados no coinciden")
            checked += ln
            self.on_progress(ln)
        return checked
