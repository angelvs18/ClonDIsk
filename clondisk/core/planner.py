"""Planificación de la clonación: cómo quedará el disco destino.

Reglas principales
------------------
* El destino usa el mismo estilo de tabla (GPT/MBR) que el origen.
* Todas las particiones se alinean a 1 MiB (óptimo para SSD), salvo en
  modo *idénticos + MBR*, donde se conservan los offsets originales porque
  Windows identifica los volúmenes MBR por firma + offset.
* Las particiones pequeñas (EFI, MSR, recuperación, etc.) conservan su tamaño.
* Una partición NTFS de datos (normalmente C:) se amplía o se ajusta al
  espacio disponible. Si el destino es más pequeño, hay que reducir antes
  la partición de origen (Windows lo hace de forma segura con sus propias
  herramientas).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .gpt import new_guid, usable_range
from .model import Disk, GiB, MiB, Partition, align_down, align_up

# Lectura secuencial típica de un HDD cuando no se ha medido (bytes/s): rango lento–rápido.
HDD_READ_RANGE = (80 * MiB, 130 * MiB)
POST_COPY_MINUTES = 2      # tabla, bcdboot, mbr2gpt, comprobaciones, TRIM


def estimate_minutes(copy_bytes: int, verify: bool,
                     read_bps: Optional[float] = None) -> Tuple[int, int]:
    """Rango (mín, máx) de minutos. Verificar vuelve a leer todo: dobla el tiempo de lectura.
    Con una velocidad medida (``read_bps``) el rango se estrecha; el interior de un HDD es
    más lento que su principio, de ahí el margen hacia abajo."""
    passes = 2 if verify else 1
    if read_bps and read_bps > 0:
        slow, fast = read_bps * 0.75, read_bps
    else:
        slow, fast = HDD_READ_RANGE
    lo = copy_bytes * passes / fast / 60
    hi = copy_bytes * passes / slow / 60 + POST_COPY_MINUTES
    return max(1, math.floor(lo)), max(1, math.ceil(hi))


def fmt_minutes_range(lo: int, hi: int) -> str:
    if lo >= hi:
        return f"~{lo} min"
    if hi >= 120:
        return f"{lo / 60:.1f}–{hi / 60:.1f} h"
    return f"{lo}–{hi} min"


def fmt_shortfall(n_bytes: int) -> str:
    """Espacio que falta, redondeado hacia arriba: «354 GB», «512 MB»."""
    if n_bytes >= GiB:
        return f"{math.ceil(n_bytes / GiB)} GB"
    return f"{max(1, math.ceil(n_bytes / MiB))} MB"


UNSUPPORTED_ROLES = {
    "ldm": "Los discos dinámicos (LDM) no son compatibles",
    "spaces": "Los Espacios de almacenamiento no son compatibles",
    "extended": "Las particiones extendidas/lógicas MBR no son compatibles",
}


@dataclass
class CloneOptions:
    smart_copy: bool = True       # copiar solo clústeres usados en NTFS
    use_vss: bool = True          # instantáneas VSS para volúmenes montados
    verify: bool = False          # releer y comparar tras copiar
    id_mode: str = "new"          # "new" (nuevos identificadores) | "same" (idénticos)
    fix_boot: bool = True         # bcdboot + MountedDevices (modo "new")
    retrim: bool = True           # TRIM al final en el destino
    dry_run: bool = False         # simulación sin escribir
    skip_bad_sectors: bool = False  # sectores ilegibles → ceros (si no, se aborta)
    backup_drivers: bool = False  # exportar drivers del Windows de origen (asistente)
    convert_to_gpt: bool = False  # origen MBR → mbr2gpt al terminar (GPT/UEFI)
    new_hardware: bool = False    # habilitar drivers de almacenamiento de arranque


@dataclass
class PlannedPartition:
    src: Partition
    new_index: int
    new_start_lba: int
    new_sectors: int
    new_unique_guid: Optional[str] = None
    copy_mode: str = "raw"        # "smart" | "raw"
    resizable: bool = False

    @property
    def extend_fs(self) -> bool:
        return self.new_sectors > self.src.sectors and self.src.fs == "NTFS"

    @property
    def moved(self) -> bool:
        return self.new_start_lba != self.src.start_lba

    def copy_sectors(self) -> int:
        return min(self.src.sectors, self.new_sectors)


@dataclass
class ClonePlan:
    source: Disk
    target: Disk
    options: CloneOptions
    parts: List[PlannedPartition] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    resizable_index: Optional[int] = None
    resize_min_sectors: int = 0
    resize_max_sectors: int = 0
    deficit_sectors: int = 0      # > 0 si el destino es demasiado pequeño
    new_disk_guid: Optional[str] = None
    new_signature: Optional[int] = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def sector_size(self) -> int:
        return self.source.sector_size

    @property
    def firmware(self) -> str:
        """Firmware para el que se ejecuta bcdboot (antes de una posible conversión)."""
        return "UEFI" if self.source.style == "GPT" else "BIOS"

    @property
    def converts_to_gpt(self) -> bool:
        return self.options.convert_to_gpt and self.source.style == "MBR"

    @property
    def final_firmware(self) -> str:
        return "UEFI" if self.converts_to_gpt else self.firmware

    def by_src_index(self, index: int) -> Optional[PlannedPartition]:
        for pp in self.parts:
            if pp.src.index == index:
                return pp
        return None

    def windows_part(self) -> Optional[PlannedPartition]:
        for pp in self.parts:
            if pp.src.has_windows or pp.src.is_boot:
                return pp
        return None

    def system_part(self) -> Optional[PlannedPartition]:
        if self.source.style == "GPT":
            for pp in self.parts:
                if pp.src.role == "efi":
                    return pp
        else:
            for pp in self.parts:
                if pp.src.active:
                    return pp
        return None

    def estimated_copy_bytes(self) -> int:
        ss = self.sector_size
        total = 0
        for pp in self.parts:
            full = pp.copy_sectors() * ss
            if pp.copy_mode == "smart" and pp.src.used_bytes is not None:
                total += min(full, pp.src.used_bytes + 2 * MiB)
            else:
                total += full
        return total

    def final_ids(self, identical: bool):
        """Identificadores finales (disco y particiones) según el modo."""
        if identical:
            return (self.source.disk_guid, self.source.signature,
                    {pp.new_index: pp.src.unique_guid for pp in self.parts})
        return (self.new_disk_guid, self.new_signature,
                {pp.new_index: pp.new_unique_guid for pp in self.parts})


def default_resizable(disk: Disk) -> Optional[int]:
    cands = [p for p in disk.partitions if p.fs == "NTFS" and p.role == "data"]
    if not cands:
        return None
    # Preferir la partición con Windows, si no la más grande.
    for p in cands:
        if p.has_windows or p.is_boot:
            return p.index
    return max(cands, key=lambda p: p.sectors).index


def shrink_target_bytes(plan: ClonePlan) -> Optional[int]:
    """Tamaño (bytes, múltiplo de 1 MiB) al que hay que reducir la partición ajustable
    del origen para que quepa en el destino; ``None`` si no hace falta o no se puede."""
    if not plan.deficit_sectors or plan.resizable_index is None:
        return None
    part = plan.source.partition_by_index(plan.resizable_index)
    if part is None:
        return None
    ss = plan.source.sector_size
    return ((part.sectors - plan.deficit_sectors) * ss // MiB) * MiB


def resizable_candidates(disk: Disk) -> List[Partition]:
    return [p for p in disk.partitions if p.fs == "NTFS" and p.role == "data"]


def build_plan(source: Disk, target: Disk, options: CloneOptions,
               resizable_index: Optional[int] = -1,
               resize_sectors: Optional[int] = None) -> ClonePlan:
    """Calcula el plan. ``resizable_index=-1`` elige automáticamente;
    ``None`` desactiva el redimensionado; ``resize_sectors=None`` = ocupar
    todo el espacio libre."""
    plan = ClonePlan(source=source, target=target, options=options)
    err, warn = plan.errors.append, plan.warnings.append
    ss = source.sector_size

    # --- Validaciones generales ---------------------------------------------
    if source.number == target.number and source.path == target.path:
        err("El origen y el destino son el mismo disco")
    if target.is_running_system:
        err("El destino contiene el Windows en ejecución; no puede sobrescribirse")
    if source.style not in ("GPT", "MBR"):
        err("El disco de origen no tiene una tabla de particiones reconocible")
    if target.sector_size != ss:
        err(f"Tamaño de sector lógico distinto (origen {ss} B, destino "
            f"{target.sector_size} B); NTFS no puede moverse entre ellos")
    if target.is_readonly:
        warn("El destino está marcado como solo lectura; se intentará quitar ese atributo")
    for p in source.partitions:
        if p.role in UNSUPPORTED_ROLES:
            err(UNSUPPORTED_ROLES[p.role])
        if p.fs == "BitLocker":
            err(f"La partición {p.display_name()} está cifrada con BitLocker. "
                "Desactiva BitLocker (descifrado completo) antes de clonar.")
    for prob in source.problems:
        warn(prob)
    if options.convert_to_gpt and source.style == "MBR":
        if options.id_mode == "same":
            err("La conversión a GPT/UEFI requiere el modo «Nuevos identificadores»")
        elif not options.fix_boot:
            err("La conversión a GPT/UEFI requiere reconstruir el arranque (bcdboot)")
    if plan.errors:
        return plan

    total = target.size_bytes // ss
    align = max(1, MiB // ss)
    identical = options.id_mode == "same"
    preserve_offsets = identical and source.style == "MBR"

    if source.style == "GPT":
        first_usable, last_usable = usable_range(total, ss)
        limit = last_usable + 1
        first_pos = align_up(first_usable, align)
    else:
        limit = min(total, 0xFFFFFFFF + 1)
        if options.convert_to_gpt:
            # mbr2gpt necesita espacio libre al final para la GPT de respaldo.
            limit = min(limit, align_down(total, align) - align)
        first_pos = align
        if total > 0xFFFFFFFF + 1:
            warn("El destino supera 2 TiB y el origen es MBR: el espacio por encima "
                 "de 2 TiB quedará sin usar (convierte el origen a GPT para aprovecharlo)")

    parts = sorted(source.partitions, key=lambda p: p.start_lba)
    if resizable_index == -1:
        resizable_index = default_resizable(source)
    res_part = source.partition_by_index(resizable_index) if resizable_index else None
    if res_part is not None and (res_part.fs != "NTFS"):
        res_part = None
    plan.resizable_index = res_part.index if res_part else None

    def layout(sizes: Dict[int, int]):
        pos = 1 if preserve_offsets else first_pos
        out = []
        for p in parts:
            if preserve_offsets and p.start_lba >= pos:
                start = p.start_lba
            else:
                start = align_up(pos, align)
            out.append((p, start, sizes[p.index]))
            pos = start + sizes[p.index]
        return out, pos

    sizes = {p.index: p.sectors for p in parts}
    _, end_orig = layout(sizes)
    free = limit - end_orig

    if res_part is not None:
        orig = res_part.sectors
        # Máximo: todo el espacio libre, redondeado a 1 MiB para mantener la alineación.
        max_s = orig + free
        if free > 0:
            max_s = max(orig, align_down(max_s, align))
        plan.resize_max_sectors = max_s
        used = res_part.used_bytes
        # Margen de seguridad: 10 % de lo usado (entre 64 MiB y 1 GiB).
        margin = min(1024 * MiB, max(64 * MiB, (used or 0) // 10))
        min_needed = ((used + margin) // ss) if used else orig
        plan.resize_min_sectors = orig  # no se reduce aquí; ver deficit
        if free < 0:
            plan.deficit_sectors = -free
            target_size = orig + free
            if used is not None and target_size * ss < used + margin:
                err("El destino es demasiado pequeño incluso reduciendo la partición "
                    f"{res_part.display_name()} (faltan "
                    f"{fmt_shortfall((min_needed - target_size) * ss)})")
            else:
                err(f"El destino es demasiado pequeño: faltan {fmt_shortfall(-free * ss)}. "
                    f"Reduce primero la partición {res_part.display_name()} del origen "
                    "(botón «Reducir partición de origen»).")
            return plan
        chosen = max_s if resize_sectors is None else resize_sectors
        chosen = max(orig, min(chosen, max_s))
        sizes[res_part.index] = chosen
    elif free < 0:
        plan.deficit_sectors = -free
        err(f"El destino es demasiado pequeño: faltan {fmt_shortfall(-free * ss)} y no "
            "hay ninguna partición NTFS que pueda ajustarse")
        return plan

    final, end = layout(sizes)
    if end > limit:
        err("Las particiones no caben en el destino")
        return plan

    for new_i, (p, start, secs) in enumerate(final, start=1):
        pp = PlannedPartition(src=p, new_index=new_i, new_start_lba=start, new_sectors=secs)
        pp.resizable = res_part is not None and p.index == res_part.index
        if source.style == "GPT":
            pp.new_unique_guid = new_guid()
        if p.fs == "NTFS" and options.smart_copy:
            pp.copy_mode = "smart"
        plan.parts.append(pp)

    if source.style == "GPT":
        plan.new_disk_guid = new_guid()
    else:
        rnd = random.SystemRandom()
        sig = 0
        while sig == 0 or sig == source.signature:
            sig = rnd.randint(1, 0xFFFFFFFF)
        plan.new_signature = sig

    # --- Advertencias útiles -------------------------------------------------
    if any(p.start_lba % align for p in parts) and not preserve_offsets:
        warn("El origen tiene particiones desalineadas; en el destino quedarán "
             "alineadas a 1 MiB (mejor rendimiento en SSD)")
    if preserve_offsets and any(p.start_lba % align for p in parts):
        warn("Con «identificadores idénticos» en MBR se conservan los offsets "
             "originales, que no están alineados a 1 MiB. Para SSD es preferible "
             "usar «nuevos identificadores».")
    moved_after = [pp for pp in plan.parts if pp.moved and pp.src.role == "recovery"]
    if moved_after:
        warn("La partición de recuperación cambiará de posición. Después de arrancar "
             "desde el SSD ejecuta «reagentc /disable» y «reagentc /enable» para "
             "re-registrar WinRE.")
    if options.id_mode == "new" and options.fix_boot and plan.windows_part() and not plan.system_part():
        warn("No se encontró partición de sistema (EFI/activa); no se podrá reparar el arranque")
    if options.id_mode == "same":
        warn("Con «identificadores idénticos» el disco destino quedará desconectado "
             "(offline) al terminar. Retira el disco de origen antes de arrancar desde el SSD.")
    if not options.use_vss and not options.dry_run and any(p.letter for p in source.partitions):
        warn("Sin instantáneas VSS, los volúmenes montados del origen se leen mientras están en "
             "uso; si cambian durante la copia el clon puede quedar inconsistente.")
    if target.partitions:
        warn(f"Se borrarán {len(target.partitions)} partición(es) existentes en el destino")
    return plan
