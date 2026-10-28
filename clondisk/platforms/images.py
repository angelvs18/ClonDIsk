"""Plataforma de laboratorio: trata archivos de imagen como discos.

Permite probar la herramienta sin tocar discos reales:

    python -m clondisk --images origen.img destino.img
"""
from __future__ import annotations

import os
from typing import List, Tuple

from ..core.device import FileDevice
from ..core.model import Disk, Partition
from ..core.scan import scan_disk
from .base import LogFn, Platform


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

    def make_bootable(self, disk, windows_offset, system_offset, firmware, log) -> None:
        log(f"Laboratorio: se ejecutaría bcdboot ({firmware}) sobre el destino")

    def remap_mounted_devices(self, disk, windows_offset, mapping, log) -> None:
        log(f"Laboratorio: se actualizarían {len(mapping)} entradas de MountedDevices")

    def shrink_partition(self, disk, part, new_size, log) -> None:
        raise NotImplementedError("En modo laboratorio reduce la partición con ntfsresize")
