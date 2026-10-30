"""Interfaz que cada plataforma (Windows real o imágenes de laboratorio) implementa."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from ..core.device import Device
from ..core.model import Disk, Partition
from ..core.winprep import DiskHealth, DriverBackup

LogFn = Callable[[str], None]


class Platform:
    name = "base"
    supports_vss = False
    is_lab = False
    supports_offline_windows = False   # respaldo de drivers / preparar volumen sin conexión

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

    def write_denied_diagnosis(self, disk: Disk) -> str:
        """Explicación para el usuario cuando el sistema deniega la escritura en el destino."""
        from ..core.winprep import write_denied_advice
        return write_denied_advice(None, "")

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
                      firmware: str, log: LogFn, win_build: Optional[int] = None) -> None:
        log("Reparar el arranque no está disponible en esta plataforma")

    def remap_mounted_devices(self, disk: Disk, windows_offset: int,
                              mapping: List[Tuple[bytes, bytes]], log: LogFn) -> None:
        log("Actualizar MountedDevices no está disponible en esta plataforma")

    def retrim(self, disk: Disk, log: LogFn) -> None:
        pass

    def prepare_new_hardware(self, disk: Disk, windows_offset: int, log: LogFn) -> None:
        """Habilita los drivers de almacenamiento de arranque del Windows clonado."""
        log("Preparar Windows para otra computadora no está disponible en esta plataforma")

    def convert_to_gpt(self, disk: Disk, log: LogFn) -> None:
        raise NotImplementedError("La conversión a GPT/UEFI no está disponible en esta plataforma")

    def boot_file_checks(self, disk: Disk, style: str, system_offset: int, windows_offset: int,
                         log: LogFn, check_bcd: bool = True) -> List[Tuple[bool, str]]:
        """Comprobaciones de archivos de arranque (bootmgr, BCD, bcdedit). Lista de (ok, texto)."""
        log("La revisión de archivos de arranque no está disponible en esta plataforma")
        return []

    # --- información del anfitrión y del origen ----------------------------
    def host_build(self) -> Optional[int]:
        return None

    def has_mbr2gpt(self) -> bool:
        return False

    def disk_health(self, disk: Disk) -> Optional[DiskHealth]:
        return None

    def backup_drivers(self, part: Partition, log: LogFn) -> DriverBackup:
        raise NotImplementedError("El respaldo de drivers no está disponible en esta plataforma")

    def prepare_offline_volume(self, disk: Disk, part: Partition, log: LogFn) -> List[str]:
        raise NotImplementedError("Preparar el volumen no está disponible en esta plataforma")

    def shrink_partition(self, disk: Disk, part: Partition, new_size: int, log: LogFn) -> None:
        raise NotImplementedError("Reducir particiones no está disponible en esta plataforma")

    def min_partition_size(self, disk: Disk, part: Partition) -> Optional[int]:
        return None
