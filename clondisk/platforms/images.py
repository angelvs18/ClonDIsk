"""Plataforma de laboratorio: trata archivos de imagen como discos.

Permite probar la herramienta sin tocar discos reales:

    python -m clondisk --images origen.img destino.img
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from ..core.device import FileDevice
from ..core.gpt import build_gpt, parse_gpt
from ..core.mbr import build_mbr, parse_mbr
from ..core.model import MiB, Disk, Partition
from ..core.scan import scan_disk
from .base import LogFn, Platform


def _copy_region(src_path: str, src_off: int, dst_path: str, dst_off: int, size: int):
    with open(src_path, "rb") as s, open(dst_path, "r+b" if os.path.exists(dst_path) else "wb") as d:
        s.seek(src_off)
        d.seek(dst_off)
        remaining = size
        while remaining:
            b = s.read(min(remaining, 8 * MiB))
            if not b:
                raise IOError(f"Fin de archivo inesperado en {src_path}")
            d.write(b)
            remaining -= len(b)


class ImagePlatform(Platform):
    name = "Laboratorio (imágenes)"
    supports_vss = False
    is_lab = True

    def __init__(self, paths: List[str], sector_size: int = 512):
        self.paths = [os.path.abspath(p) for p in paths]
        self.sector_size = sector_size

    def list_disks(self) -> List[Disk]:
        disks = []
        for i, path in enumerate(self.paths):
            with FileDevice(path, sector_size=self.sector_size) as dev:
                d = scan_disk(dev, i, model=os.path.basename(path))
            d.media = "Imagen"
            d.bus = "Archivo"
            disks.append(d)
        return disks

    def open_disk(self, disk: Disk, write: bool = False):
        return FileDevice(disk.path, writable=write, sector_size=self.sector_size)

    def prepare_target(self, disk: Disk, log: LogFn) -> None:
        log(f"Imagen destino lista: {disk.path}")

    def bring_online(self, disk: Disk, log: LogFn) -> None:
        log("Laboratorio: no hay que conectar el disco")

    def create_snapshot(self, part: Partition, sector_size: int, log: LogFn) -> Tuple[str, object]:
        raise NotImplementedError

    def extend_filesystem(self, disk: Disk, part_offset: int, log: LogFn) -> None:
        log("Laboratorio: la ampliación de NTFS la hace Windows (diskpart «extend filesystem»); "
            "en imágenes se omite", "warn")

    def make_bootable(self, disk, windows_offset, system_offset, firmware, log, win_build=None) -> None:
        log(f"Laboratorio: se ejecutaría bcdboot ({firmware}) sobre el destino")

    def remap_mounted_devices(self, disk, windows_offset, mapping, log) -> None:
        log(f"Laboratorio: se actualizarían {len(mapping)} entradas de MountedDevices")

    def prepare_new_hardware(self, disk, windows_offset, log) -> None:
        log("Laboratorio: se habilitarían storahci/stornvme/iaStorV/iaStorAVC en el registro")

    # ------------------------------------------------------------------ reducción con ntfsresize
    @staticmethod
    def _ntfsresize() -> Optional[str]:
        return shutil.which("ntfsresize")

    def min_partition_size(self, disk: Disk, part: Partition) -> Optional[int]:
        exe = self._ntfsresize()
        if not exe or part.fs != "NTFS":
            return None
        with tempfile.TemporaryDirectory(prefix="clondisk_lab_") as td:
            tmp = os.path.join(td, "part.bin")
            _copy_region(disk.path, part.offset_bytes(disk.sector_size), tmp, 0,
                         part.size_bytes(disk.sector_size))
            r = subprocess.run([exe, "-f", "-i", tmp], capture_output=True, text=True)
        m = re.search(r"resize at (\d+) bytes", r.stdout)
        return int(m.group(1)) if m else None

    def shrink_partition(self, disk: Disk, part: Partition, new_size: int, log: LogFn) -> None:
        """Reduce un NTFS con ``ntfsresize`` y ajusta su entrada en la tabla MBR/GPT."""
        exe = self._ntfsresize()
        if not exe:
            raise NotImplementedError("En modo laboratorio hace falta ntfsresize (ntfs-3g) para "
                                      "reducir particiones")
        ss = disk.sector_size
        if part.fs != "NTFS":
            raise ValueError("Solo se pueden reducir particiones NTFS")
        if new_size % ss or not 0 < new_size < part.size_bytes(ss):
            raise ValueError(f"Tamaño inválido para reducir {part.display_name()}: {new_size}")
        off = part.offset_bytes(ss)
        with tempfile.TemporaryDirectory(prefix="clondisk_lab_") as td:
            tmp = os.path.join(td, "part.bin")
            _copy_region(disk.path, off, tmp, 0, part.size_bytes(ss))
            r = subprocess.run([exe, "-f", "-s", str(new_size), tmp], input="y\n",
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"ntfsresize falló: {(r.stdout + r.stderr).strip()}")
            _copy_region(tmp, 0, disk.path, off, new_size)

        new_sectors = new_size // ss
        with FileDevice(disk.path, writable=True, sector_size=ss) as dev:
            total = dev.size // ss
            if disk.style == "MBR":
                code, sig, entries = parse_mbr(dev.read(0, ss))
                for e in entries:
                    if e.start_lba == part.start_lba:
                        e.sectors = new_sectors
                dev.write(0, build_mbr(code, sig, entries, ss))
            elif disk.style == "GPT":
                code = dev.read(0, ss)[:440]
                table = parse_gpt(dev.read, ss, total)
                for e in table.entries:
                    if e.first_lba == part.start_lba:
                        e.last_lba = part.start_lba + new_sectors - 1
                for o, data in build_gpt(table.disk_guid, table.entries, total, ss, code):
                    dev.write(o, data)
            else:
                raise ValueError("El disco no tiene una tabla de particiones reconocible")
            dev.flush()
        log(f"Partición {part.display_name()} reducida a {new_size} bytes con ntfsresize")
