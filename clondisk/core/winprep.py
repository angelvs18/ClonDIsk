"""Decisiones sobre el Windows clonado, independientes del sistema operativo.

Aquí está la lógica que se puede probar sin discos reales ni ``winreg``:

* salud del disco de origen (contadores de fiabilidad);
* si se puede convertir el destino a GPT/UEFI con ``mbr2gpt``;
* qué ``bcdboot.exe`` usar (el del anfitrión o el del Windows clonado);
* qué carpetas del DriverStore corresponden a los ``oem*.inf`` (respaldo de drivers);
* habilitar los drivers de almacenamiento de arranque en una colmena SYSTEM;
* interpretar la salida de ``bcdedit``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .model import Disk, Partition

MBR2GPT_MIN_HOST_BUILD = 15063     # Windows 10 1703
WIN10_FIRST_BUILD = 10240


# ---------------------------------------------------------------------------
# Salud del disco
# ---------------------------------------------------------------------------
@dataclass
class DiskHealth:
    health_status: Optional[str] = None        # Healthy | Warning | Unhealthy | ...
    read_errors_uncorrected: Optional[int] = None
    temperature: Optional[int] = None          # °C
    wear: Optional[int] = None                 # % de desgaste (SSD)
    counters_available: bool = False


def assess_health(h: Optional[DiskHealth]) -> Tuple[str, List[str]]:
    """Devuelve (nivel, mensajes). Nivel: ``ok`` | ``danger`` | ``unknown``."""
    if h is None or (h.health_status is None and not h.counters_available):
        return "unknown", ["No se pudo consultar la salud del disco (es habitual con algunos "
                           "adaptadores USB). No se bloquea nada, pero no hay garantías."]
    msgs: List[str] = []
    level = "ok"
    status = (h.health_status or "").strip()
    if status and status.lower() != "healthy":
        level = "danger"
        msgs.append(f"Windows informa que el disco NO está sano (estado: {status}).")
    if h.read_errors_uncorrected:
        level = "danger"
        msgs.append(f"El disco tiene {h.read_errors_uncorrected} error(es) de lectura no corregidos.")
    if not h.counters_available:
        msgs.append("Los contadores de fiabilidad (errores, temperatura, desgaste) no están "
                    "disponibles (es habitual con adaptadores USB).")
    else:
        det = []
        if h.temperature:
            det.append(f"temperatura {h.temperature} °C")
        if h.wear is not None:
            det.append(f"desgaste {h.wear} %")
        det.append(f"errores de lectura no corregidos: {h.read_errors_uncorrected or 0}")
        msgs.append("Contadores: " + ", ".join(det) + ".")
    if level == "ok" and status:
        msgs.insert(0, "Estado del disco: Healthy.")
    return level, msgs


# ---------------------------------------------------------------------------
# Windows del origen
# ---------------------------------------------------------------------------
def source_windows(disk: Disk) -> Optional[Partition]:
    for p in disk.partitions:
        if p.has_windows or p.is_boot:
            return p
    return None


def offline_windows(disk: Disk) -> Optional[Partition]:
    """Partición con un Windows que NO es el que está en ejecución."""
    p = source_windows(disk)
    return p if p is not None and p.has_windows and not p.is_boot else None


# ---------------------------------------------------------------------------
# Conversión MBR → GPT (mbr2gpt)
# ---------------------------------------------------------------------------
def gpt_conversion_status(source: Disk, host_build: Optional[int],
                          has_mbr2gpt: bool) -> Tuple[bool, str]:
    """¿Se puede ofrecer la conversión a GPT/UEFI? Devuelve (permitida, motivo)."""
    if source.style != "MBR":
        return False, "El origen ya es GPT."
    if host_build is None or host_build < MBR2GPT_MIN_HOST_BUILD:
        hb = host_build if host_build is not None else "desconocido"
        return False, (f"Este equipo tiene Windows build {hb}; mbr2gpt necesita Windows 10 "
                       "1703 (build 15063) o posterior en el equipo que ejecuta ClonDisk.")
    if not has_mbr2gpt:
        return False, "No se encontró mbr2gpt.exe en System32 de este equipo."
    if len(source.partitions) > 3:
        return False, (f"El origen tiene {len(source.partitions)} particiones primarias; mbr2gpt "
                       "admite como máximo 3 (necesita un hueco para crear la partición EFI).")
    win = source_windows(source)
    legacy = ("El SSD tendrá que arrancar en modo BIOS/Legacy (CSM): el equipo de destino "
              "debe tener Legacy/CSM habilitado.")
    if win is None:
        return False, "No se detectó Windows en el origen. " + legacy
    info = win.win_info
    if info is None or info.build is None:
        return False, "No se pudo determinar la versión del Windows de origen. " + legacy
    if not info.is_win10_or_later:
        return False, (f"El Windows de origen ({info.display()}) es anterior a Windows 10 y no "
                       "admite conversión con mbr2gpt. " + legacy)
    return True, ""


# ---------------------------------------------------------------------------
# bcdboot
# ---------------------------------------------------------------------------
def choose_bcdboot(host_build: Optional[int], win_build: Optional[int], host_exe: str,
                   target_exe: str, target_exists: bool) -> Tuple[str, str]:
    """Elige el ``bcdboot.exe``: el del Windows clonado si es más nuevo que el anfitrión."""
    if (win_build is not None and host_build is not None and win_build > host_build
            and target_exists):
        return target_exe, (f"se usa el bcdboot del Windows clonado (build {win_build} > "
                            f"anfitrión {host_build})")
    if win_build is not None and host_build is not None and win_build > host_build:
        return host_exe, (f"el Windows clonado (build {win_build}) es más nuevo que el anfitrión "
                          f"({host_build}) pero no tiene bcdboot.exe; se usa el del anfitrión")
    return host_exe, f"se usa el bcdboot del anfitrión (build {host_build or '?'})"


# ---------------------------------------------------------------------------
# Respaldo de drivers (alternativa a DISM)
# ---------------------------------------------------------------------------
@dataclass
class DriverBackup:
    folder: str
    count: int
    method: str                    # "dism" | "driverstore"
    unmatched: List[str] = field(default_factory=list)


def match_oem_driver_folders(oem: Dict[str, str],
                             repo: Dict[str, Iterable[str]]) -> Tuple[Dict[str, str], List[str]]:
    """Empareja cada ``oemNN.inf`` (nombre → huella del contenido) con la carpeta de
    ``FileRepository`` que contiene un .inf idéntico. Devuelve (oem → carpeta, sin pareja)."""
    by_digest: Dict[str, str] = {}
    for folder in sorted(repo):
        for digest in repo[folder]:
            by_digest.setdefault(digest, folder)
    found: Dict[str, str] = {}
    missing: List[str] = []
    for name in sorted(oem, key=str.lower):
        folder = by_digest.get(oem[name])
        if folder:
            found[name] = folder
        else:
            missing.append(name)
    return found, missing


# ---------------------------------------------------------------------------
# Drivers de almacenamiento de arranque (otra computadora)
# ---------------------------------------------------------------------------
BOOT_STORAGE_SERVICES = ("storahci", "stornvme", "iaStorV", "iaStorAVC")


class Hive:
    """Interfaz mínima sobre una colmena de registro cargada (rutas relativas a la raíz)."""

    def key_exists(self, path: str) -> bool:  # pragma: no cover - interfaz
        raise NotImplementedError

    def get_value(self, path: str, name: str):  # pragma: no cover - interfaz
        raise NotImplementedError

    def set_dword(self, path: str, name: str, value: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete_key(self, path: str) -> None:  # pragma: no cover - interfaz (recursivo)
        raise NotImplementedError


def enable_boot_storage_drivers(hive: Hive, log: Callable[[str], None] = lambda m: None) -> List[str]:
    """Pone Start=0 en los drivers de almacenamiento de arranque del ControlSet actual y
    borra sus ``StartOverride``. Evita INACCESSIBLE_BOOT_DEVICE con otra controladora."""
    current = hive.get_value("Select", "Current")
    if not isinstance(current, int) or current <= 0:
        raise ValueError("La colmena SYSTEM no tiene un valor Select\\Current válido")
    cs = f"ControlSet{current:03d}"
    changes: List[str] = []
    for svc in BOOT_STORAGE_SERVICES:
        key = f"{cs}\\Services\\{svc}"
        if not hive.key_exists(key):
            continue
        old = hive.get_value(key, "Start")
        if old != 0:
            hive.set_dword(key, "Start", 0)
            changes.append(f"{key}: Start {old} → 0")
        if hive.key_exists(key + "\\StartOverride"):
            hive.delete_key(key + "\\StartOverride")
            changes.append(f"{key}\\StartOverride eliminado")
    for c in changes:
        log(c)
    return changes


# ---------------------------------------------------------------------------
# bcdedit
# ---------------------------------------------------------------------------
def bcdedit_partition_letters(output: str) -> Set[str]:
    """Letras que aparecen como ``partition=X:`` (device/osdevice) en ``bcdedit /enum``.
    No depende del idioma: los nombres de campo se traducen, el valor no."""
    return {m.upper() for m in re.findall(r"partition=([A-Za-z]):", output)}
