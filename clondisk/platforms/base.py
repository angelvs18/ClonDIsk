"""Interfaz que cada plataforma (Windows real o imágenes de laboratorio) implementa."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from ..core.device import Device
from ..core.model import Disk, Partition

LogFn = Callable[[str], None]


class Platform:
    name = "base"
    supports_vss = False
    is_lab = False

    def list_disks(self) -> List[Disk]:
        raise NotImplementedError

    def open_disk(self, disk: Disk, write: bool = False) -> Device:
        raise NotImplementedError

    # --- ciclo de vida del destino ----------------------------------------
    def prepare_target(self, disk: Disk, log: LogFn) -> None:
        """Deja el destino listo para escritura directa (offline, sin solo-lectura)."""

    def refresh_target(self, disk: Disk, log: LogFn) -> None:
        """Pide al sistema que relea la tabla de particiones."""

    def bring_online(self, disk: Disk, log: LogFn) -> None:
        pass

    def set_offline(self, disk: Disk, log: LogFn) -> None:
        pass

    # --- instantáneas -------------------------------------------------------
    def create_snapshot(self, part: Partition, sector_size: int, log: LogFn) -> Tuple[str, Device]:
        raise NotImplementedError

    def delete_snapshot(self, snap_id: str, log: LogFn) -> None:
        pass

    # --- posprocesamiento ---------------------------------------------------
    def extend_filesystem(self, disk: Disk, part_offset: int, log: LogFn) -> None:
        log("Ampliar el sistema de archivos no está disponible en esta plataforma")

    def make_bootable(self, disk: Disk, windows_offset: int, system_offset: int,
                      firmware: str, log: LogFn) -> None:
        log("Reparar el arranque no está disponible en esta plataforma")

    def remap_mounted_devices(self, disk: Disk, windows_offset: int,
                              mapping: List[Tuple[bytes, bytes]], log: LogFn) -> None:
        log("Actualizar MountedDevices no está disponible en esta plataforma")

    def retrim(self, disk: Disk, log: LogFn) -> None:
        pass

    def shrink_partition(self, disk: Disk, part: Partition, new_size: int, log: LogFn) -> None:
        raise NotImplementedError("Reducir particiones no está disponible en esta plataforma")

    def min_partition_size(self, disk: Disk, part: Partition) -> Optional[int]:
        return None
