"""Modelo de datos: discos, particiones y utilidades de tamaño."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB
TiB = 1024 * GiB

# --- Tipos de partición GPT conocidos -------------------------------------
GUID_EFI = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GUID_MSR = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
GUID_BASIC = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
GUID_RECOVERY = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"
GUID_LDM_META = "5808C8AA-7E8F-42E0-85D2-E1E90434CFB3"
GUID_LDM_DATA = "AF9B60A0-1431-4F62-BC68-3311714A69AD"
GUID_STORAGE_SPACES = "E75CAF8F-F680-4CEE-AFA3-B001E56EFC2D"
GUID_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"

GPT_TYPE_NAMES = {
    GUID_EFI: "Sistema EFI",
    GUID_MSR: "Reservada de Microsoft (MSR)",
    GUID_BASIC: "Datos básicos",
    GUID_RECOVERY: "Recuperación de Windows",
    GUID_LDM_META: "Metadatos LDM (disco dinámico)",
    GUID_LDM_DATA: "Datos LDM (disco dinámico)",
    GUID_STORAGE_SPACES: "Espacios de almacenamiento",
    GUID_LINUX: "Linux",
}

# --- Tipos de partición MBR conocidos --------------------------------------
MBR_EXTENDED = {0x05, 0x0F, 0x85}
MBR_PROTECTIVE = 0xEE
MBR_TYPE_NAMES = {
    0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x0E: "FAT16 (LBA)",
    0x07: "NTFS / exFAT", 0x0B: "FAT32", 0x0C: "FAT32 (LBA)",
    0x27: "Recuperación de Windows", 0x42: "LDM (disco dinámico)",
    0x05: "Extendida", 0x0F: "Extendida (LBA)", 0x85: "Extendida (Linux)",
    0x82: "Linux swap", 0x83: "Linux", 0xEF: "Sistema EFI", 0xEE: "GPT protector",
}


def fmt_size(n: Optional[int]) -> str:
    """Formatea bytes en unidades binarias legibles (como el Administrador de discos)."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.2f} {unit}" if n < 100 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def align_up(value: int, align: int) -> int:
    return ((value + align - 1) // align) * align


def align_down(value: int, align: int) -> int:
    return (value // align) * align


@dataclass
class WindowsInfo:
    """Versión de un Windows leída de su colmena SOFTWARE."""
    product_name: str = ""
    build: Optional[int] = None
    edition: str = ""

    @property
    def is_win10_or_later(self) -> bool:
        return self.build is not None and self.build >= 10240

    def display(self) -> str:
        name = self.product_name or "Windows"
        # Windows 11 sigue declarando «Windows 10» en ProductName.
        if self.build is not None and self.build >= 22000 and name.startswith("Windows 10"):
            name = "Windows 11" + name[len("Windows 10"):]
        extra = [f"build {self.build}" if self.build is not None else "build ?"]
        if self.edition:
            extra.append(self.edition)
        return f"{name} ({', '.join(extra)})"


@dataclass
class Partition:
    index: int                      # posición 1-based en la tabla
    start_lba: int
    sectors: int
    type_guid: Optional[str] = None  # GPT
    unique_guid: Optional[str] = None
    attributes: int = 0
    name: str = ""
    mbr_type: Optional[int] = None   # MBR
    active: bool = False
    # Información del sistema de archivos / sistema operativo
    fs: str = "?"
    letter: Optional[str] = None
    label: str = ""
    used_bytes: Optional[int] = None
    volume_path: Optional[str] = None
    has_windows: bool = False
    is_boot: bool = False      # contiene el Windows en ejecución
    is_system: bool = False    # partición de arranque (EFI / activa)
    win_info: Optional[WindowsInfo] = None  # versión del Windows que contiene

    # ------------------------------------------------------------------
    @property
    def end_lba(self) -> int:
        """Primer sector *después* de la partición (exclusivo)."""
        return self.start_lba + self.sectors

    def offset_bytes(self, sector_size: int) -> int:
        return self.start_lba * sector_size

    def size_bytes(self, sector_size: int) -> int:
        return self.sectors * sector_size

    @property
    def role(self) -> str:
        if self.type_guid:
            t = self.type_guid.upper()
            return {
                GUID_EFI: "efi", GUID_MSR: "msr", GUID_RECOVERY: "recovery",
                GUID_BASIC: "data", GUID_LDM_META: "ldm", GUID_LDM_DATA: "ldm",
                GUID_STORAGE_SPACES: "spaces",
            }.get(t, "other")
        if self.mbr_type is not None:
            if self.mbr_type in MBR_EXTENDED:
                return "extended"
            if self.mbr_type == 0x42:
                return "ldm"
            if self.mbr_type == 0x27:
                return "recovery"
            if self.mbr_type == 0xEF:
                return "efi"
            if self.mbr_type in (0x07, 0x0B, 0x0C, 0x06, 0x0E, 0x04, 0x01):
                # Las particiones de recuperación MBR suelen ser 0x27, pero
                # algunos fabricantes usan 0x07 con etiqueta "Recovery".
                return "data"
        return "other"

    @property
    def type_name(self) -> str:
        if self.type_guid:
            return GPT_TYPE_NAMES.get(self.type_guid.upper(), f"GPT {self.type_guid}")
        if self.mbr_type is not None:
            return MBR_TYPE_NAMES.get(self.mbr_type, f"MBR 0x{self.mbr_type:02X}")
        return "Desconocido"

    def display_name(self) -> str:
        role = self.role
        if self.letter:
            base = f"{self.letter}:"
            return f"{base} {self.label}".strip() if self.label else base
        if self.label:
            return self.label
        return {
            "efi": "Sistema EFI", "msr": "Reservada MSR", "recovery": "Recuperación",
        }.get(role, self.name or f"Partición {self.index}")


@dataclass
class Disk:
    number: int
    path: str
    model: str
    size_bytes: int
    sector_size: int = 512
    physical_sector_size: int = 512
    style: str = "RAW"              # GPT | MBR | RAW
    disk_guid: Optional[str] = None
    signature: Optional[int] = None
    partitions: List[Partition] = field(default_factory=list)
    media: str = "Desconocido"      # SSD | HDD | Desconocido
    bus: str = ""
    serial: str = ""
    is_system: bool = False         # contiene la partición de arranque en uso
    is_boot: bool = False           # contiene el Windows en ejecución
    is_offline: bool = False
    is_readonly: bool = False
    boot_code: bytes = b""          # primeros 440 bytes del MBR
    problems: List[str] = field(default_factory=list)

    @property
    def total_sectors(self) -> int:
        return self.size_bytes // self.sector_size

    @property
    def is_running_system(self) -> bool:
        return self.is_system or self.is_boot

    def title(self) -> str:
        style = {"GPT": "Disco GPT", "MBR": "Disco MBR"}.get(self.style, "Disco sin inicializar")
        media = f" · {self.media}" if self.media and self.media != "Desconocido" else ""
        bus = f" · {self.bus}" if self.bus else ""
        return f"{style} {self.number} – {self.model} <{fmt_size(self.size_bytes)}>{media}{bus}"

    def partition_by_index(self, index: int) -> Optional[Partition]:
        for p in self.partitions:
            if p.index == index:
                return p
        return None
