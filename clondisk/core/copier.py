"""Copia por rangos con lectura y escritura en paralelo (pipeline).

Un hilo lee del origen mientras el hilo principal escribe en el destino, de
modo que un HDD lento y un SSD rápido trabajan a la vez.
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
                 chunk_size: int = 8 * MiB, queue_depth: int = 4):
        self.cancel = cancel
        self.on_progress = on_progress or (lambda n: None)
        self.chunk = chunk_size
        self.depth = queue_depth

    def _check(self):
        if self.cancel.is_set():
            raise CancelledError("Operación cancelada por el usuario")

    def copy(self, src: Device, src_base: int, dst: Device, dst_base: int,
             ranges: List[Range]) -> int:
        """Copia ``ranges`` (relativos a la partición) de src a dst."""
        chunks = split_ranges(ranges, self.chunk)
        q: "queue.Queue" = queue.Queue(maxsize=self.depth)
        stop = threading.Event()
        error: List[BaseException] = []

        def reader():
            try:
                for off, ln in chunks:
                    if stop.is_set() or self.cancel.is_set():
                        break
                    data = src.read(src_base + off, ln)
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
               ranges: List[Range]) -> int:
        checked = 0
        for off, ln in split_ranges(ranges, self.chunk):
            self._check()
            a = src.read(src_base + off, ln)
            b = dst.read(dst_base + off, ln)
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
