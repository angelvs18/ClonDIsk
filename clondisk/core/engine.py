"""Orquestador de la clonación.

Secuencia (modo «nuevos identificadores»):

1. Validar el plan y preparar el destino (offline, escribible).
2. Crear instantáneas VSS de los volúmenes montados del origen.
3. Calcular qué rangos copiar (bitmap NTFS o sector a sector).
4. Borrar las tablas antiguas del destino y copiar cada partición a su nueva posición.
5. Verificar (opcional) y corregir «hidden sectors» de particiones movidas.
6. Escribir la tabla de particiones (al final: si se cancela, el destino queda vacío).
7. Conectar el destino, ampliar NTFS en las particiones agrandadas.
8. Reconstruir el arranque (bcdboot) y actualizar MountedDevices del Windows clonado.
9. TRIM del SSD.

En modo «identificadores idénticos» se trabaja con IDs temporales y al final
se escriben los originales con el disco ya offline (evita colisiones de firma).
"""
from __future__ import annotations

import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .copier import CancelledError, Copier, VerifyError
from .device import Device
from .fs import FsError, ntfs_used_ranges, patch_hidden_sectors
from .gpt import GptEntry, build_gpt
from .mbr import MbrEntry, build_mbr
from .model import MiB, fmt_size
from .planner import ClonePlan, PlannedPartition

STEPS = [
    "Validar plan",
    "Preparar disco destino",
    "Crear instantáneas (VSS)",
    "Analizar particiones",
    "Copiar datos",
    "Verificar copia",
    "Escribir tabla de particiones",
    "Conectar destino y ampliar volúmenes",
    "Configurar arranque",
    "Finalizar",
]


@dataclass
class CloneResult:
    success: bool
    message: str
    cancelled: bool = False
    elapsed: float = 0.0
    copied_bytes: int = 0
    warnings: List[str] = field(default_factory=list)


class CloneEngine:
    def __init__(self, platform, plan: ClonePlan,
                 log: Optional[Callable[[str, str], None]] = None,
                 step: Optional[Callable[[int, str], None]] = None,
                 progress: Optional[Callable[[int, int, float], None]] = None,
                 cancel: Optional[threading.Event] = None):
        self.platform = platform
        self.plan = plan
        self._log = log or (lambda msg, level="info": print(f"[{level}] {msg}"))
        self._step = step or (lambda i, name: None)
        self._progress = progress or (lambda done, total, speed: None)
        self.cancel = cancel or threading.Event()
        self.snapshots: List[str] = []
        self.snap_devices: Dict[int, Device] = {}
        self.ranges: Dict[int, List[Tuple[int, int]]] = {}
        self.done = 0
        self.total = 0
        self._t0 = 0.0
        self._last_emit = 0.0
        self._copy_t0 = time.monotonic()
        self.warnings: List[str] = []

    # ------------------------------------------------------------------
    def log(self, msg: str, level: str = "info"):
        if level == "warn":
            self.warnings.append(msg)
        self._log(msg, level)

    def step(self, i: int):
        self._step(i, STEPS[i])
        self.log(f"— {STEPS[i]} —", "step")

    def _check_cancel(self):
        if self.cancel.is_set():
            raise CancelledError("Operación cancelada por el usuario")

    def _on_bytes(self, n: int):
        self.done += n
        now = time.monotonic()
        if now - self._last_emit >= 0.25 or self.done >= self.total:
            self._last_emit = now
            elapsed = max(now - self._copy_t0, 1e-6)
            self._progress(self.done, self.total, self.done / elapsed)

    # ------------------------------------------------------------------
    def run(self) -> CloneResult:
        self._t0 = time.monotonic()
        plan, opts = self.plan, self.plan.options
        dry = opts.dry_run
        src = dst = None
        try:
            self.step(0)
            if not plan.ok:
                raise RuntimeError("El plan tiene errores: " + "; ".join(plan.errors))
            for w in plan.warnings:
                self.log(w, "warn")
            self.log(f"Origen: {plan.source.title()}")
            self.log(f"Destino: {plan.target.title()}")
            if dry:
                self.log("MODO SIMULACIÓN: no se escribirá nada en ningún disco", "warn")

            self.step(1)
            if not dry:
                self.platform.prepare_target(plan.target, self.log)
            self._check_cancel()

            self.step(2)
            if opts.use_vss and self.platform.supports_vss and not dry:
                for pp in plan.parts:
                    p = pp.src
                    # Solo volúmenes con letra: son los que pueden estar en uso.
                    # Recuperación/EFI no montados se leen directamente del disco.
                    if p.fs != "NTFS" or not p.letter:
                        continue
                    try:
                        sid, dev = self.platform.create_snapshot(p, plan.sector_size, self.log)
                    except Exception as e:
                        if p.is_boot or p.has_windows:
                            raise  # copiar Windows en uso sin instantánea no es seguro
                        self.log(f"{e} — se leerá {p.display_name()} directamente del disco", "warn")
                        continue
                    self.snapshots.append(sid)
                    self.snap_devices[pp.new_index] = dev
                    self.log(f"Instantánea creada para {p.display_name()}")
            else:
                self.log("Sin instantáneas: se leerá directamente del disco de origen")
            self._check_cancel()

            src = self.platform.open_disk(plan.source, write=False)
            self.step(3)
            self._compute_ranges(src)
            copy_total = sum(ln for r in self.ranges.values() for _, ln in r)
            self.total = copy_total * (2 if opts.verify else 1)
            self.log(f"Datos a copiar: {fmt_size(copy_total)}")
            if dry:
                return self._finish(True, "Simulación completada: el plan es válido. "
                                    f"Se copiarían {fmt_size(copy_total)}.")

            dst = self.platform.open_disk(plan.target, write=True)
            if dst.size < plan.target.size_bytes:
                raise RuntimeError("El tamaño real del destino es menor que el informado")

            self.step(4)
            self._wipe_tables(dst)
            copier = Copier(self.cancel, self._on_bytes)
            self._copy_t0 = time.monotonic()
            self._progress(0, self.total, 0.0)
            ss = plan.sector_size
            for pp in plan.parts:
                sdev, sbase = self._source_for(pp, src)
                ranges = self.ranges[pp.new_index]
                self.log(f"Copiando {pp.src.display_name()} ({pp.copy_mode == 'smart' and 'inteligente' or 'sector a sector'}, "
                         f"{fmt_size(sum(l for _, l in ranges))}) → offset {fmt_size(pp.new_start_lba * ss)}")
                copier.copy(sdev, sbase, dst, pp.new_start_lba * ss, ranges)
            dst.flush()

            self.step(5)
            if opts.verify:
                for pp in plan.parts:
                    sdev, sbase = self._source_for(pp, src)
                    copier.verify(sdev, sbase, dst, pp.new_start_lba * ss, self.ranges[pp.new_index])
                    self.log(f"Verificada {pp.src.display_name()}: OK")
            else:
                self.log("Verificación desactivada")

            self.step(6)
            for pp in plan.parts:
                if pp.moved:
                    changes = patch_hidden_sectors(dst, pp.new_start_lba * ss,
                                                   pp.new_sectors * ss, pp.new_start_lba)
                    for c in changes:
                        self.log(f"{pp.src.display_name()}: {c}")
            identical = opts.id_mode == "same"
            # En modo idéntico se escriben primero IDs temporales (los nuevos).
            self._write_table(dst, plan.final_ids(identical=False))
            dst.flush()
            dst.close()
            dst = None
            self.platform.refresh_target(plan.target, self.log)
            self.log("Tabla de particiones escrita")
            self._release_snapshots()

            self.step(7)
            self.platform.bring_online(plan.target, self.log)
            for pp in plan.parts:
                if pp.extend_fs:
                    self._check_cancel()
                    self.log(f"Ampliando NTFS de {pp.src.display_name()} a {fmt_size(pp.new_sectors * ss)}")
                    try:
                        self.platform.extend_filesystem(plan.target, pp.new_start_lba * ss, self.log)
                    except Exception as e:  # no es fatal: se puede ampliar a mano
                        self.log(f"No se pudo ampliar el volumen: {e}. Puedes hacerlo luego en "
                                 "Administración de discos → Extender volumen.", "warn")

            self.step(8)
            self._configure_boot()

            self.step(9)
            if opts.retrim:
                try:
                    self.platform.retrim(plan.target, self.log)
                except Exception as e:
                    self.log(f"TRIM no disponible: {e}", "warn")
            if identical:
                self.platform.set_offline(plan.target, self.log)
                dst = self.platform.open_disk(plan.target, write=True)
                self._write_table(dst, plan.final_ids(identical=True))
                dst.flush()
                dst.close()
                dst = None
                self.log("Identificadores originales restaurados en el destino; el disco "
                         "queda offline. Retira el disco de origen antes de arrancar.", "warn")
            return self._finish(True, "Clonación completada correctamente")
        except CancelledError as e:
            self.log(str(e), "warn")
            return self._finish(False, "Clonación cancelada. El destino no tiene una tabla "
                                "de particiones válida; puedes volver a intentarlo.", cancelled=True)
        except (VerifyError, FsError, Exception) as e:
            self.log(f"ERROR: {e}", "error")
            return self._finish(False, f"La clonación falló: {e}")
        finally:
            for d in (dst, src):
                if d is not None:
                    try:
                        d.close()
                    except Exception:
                        pass
            for d in self.snap_devices.values():
                try:
                    d.close()
                except Exception:
                    pass
            self._release_snapshots()

    # ------------------------------------------------------------------
    def _finish(self, ok: bool, msg: str, cancelled: bool = False) -> CloneResult:
        elapsed = time.monotonic() - self._t0
        self.log(msg, "ok" if ok else ("warn" if cancelled else "error"))
        return CloneResult(ok, msg, cancelled, elapsed, self.done, list(self.warnings))

    def _release_snapshots(self):
        for d in self.snap_devices.values():
            try:
                d.close()
            except Exception:
                pass
        self.snap_devices.clear()
        while self.snapshots:
            sid = self.snapshots.pop()
            try:
                self.platform.delete_snapshot(sid, self.log)
            except Exception as e:
                self.log(f"No se pudo eliminar la instantánea {sid}: {e}", "warn")

    def _source_for(self, pp: PlannedPartition, src: Device) -> Tuple[Device, int]:
        if pp.new_index in self.snap_devices:
            return self.snap_devices[pp.new_index], 0
        return src, pp.src.start_lba * self.plan.sector_size

    def _compute_ranges(self, src: Device):
        ss = self.plan.sector_size
        for pp in self.plan.parts:
            self._check_cancel()
            size = pp.copy_sectors() * ss
            sdev, sbase = self._source_for(pp, src)
            if pp.copy_mode == "smart":
                try:
                    ranges, boot = ntfs_used_ranges(sdev, sbase, size)
                    used = sum(l for _, l in ranges)
                    self.ranges[pp.new_index] = ranges
                    self.log(f"{pp.src.display_name()}: NTFS, clúster {boot.cluster_size} B, "
                             f"{fmt_size(used)} en uso de {fmt_size(size)} ({len(ranges)} bloques)")
                    continue
                except Exception as e:
                    self.log(f"{pp.src.display_name()}: no se pudo leer el bitmap NTFS ({e}); "
                             "se copiará sector a sector", "warn")
                    pp.copy_mode = "raw"
            self.ranges[pp.new_index] = [(0, size)]
            self.log(f"{pp.src.display_name()}: {pp.src.fs}, copia sector a sector de {fmt_size(size)}")

    def _wipe_tables(self, dst: Device):
        """Borra la tabla antigua (inicio) y la GPT de respaldo (final)."""
        plan, ss = self.plan, self.plan.sector_size
        total = plan.target.size_bytes // ss
        first = min(pp.new_start_lba for pp in plan.parts) if plan.parts else 2048
        last_end = max(pp.new_start_lba + pp.new_sectors for pp in plan.parts) if plan.parts else 0
        head = min(first, (2 * MiB) // ss)
        dst.write(0, b"\0" * (head * ss))
        tail_start = max(last_end, total - (2 * MiB) // ss)
        if tail_start < total:
            dst.write(tail_start * ss, b"\0" * ((total - tail_start) * ss))

    def _write_table(self, dst: Device, ids):
        plan, ss = self.plan, self.plan.sector_size
        disk_guid, signature, part_ids = ids
        total = plan.target.size_bytes // ss
        if plan.source.style == "GPT":
            entries = [GptEntry(slot=pp.new_index - 1, type_guid=pp.src.type_guid,
                                unique_guid=part_ids[pp.new_index], first_lba=pp.new_start_lba,
                                last_lba=pp.new_start_lba + pp.new_sectors - 1,
                                attributes=pp.src.attributes, name=pp.src.name)
                       for pp in plan.parts]
            blocks = build_gpt(disk_guid, entries, total, ss, plan.source.boot_code)
        else:
            entries = [MbrEntry(slot=pp.new_index - 1, status=0x80 if pp.src.active else 0,
                                ptype=pp.src.mbr_type, start_lba=pp.new_start_lba,
                                sectors=pp.new_sectors) for pp in plan.parts]
            blocks = [(0, build_mbr(plan.source.boot_code, signature, entries, ss))]
        for off, data in blocks:
            dst.write(off, data)

    # ------------------------------------------------------------------
    def mounted_devices_mapping(self, identical: bool) -> List[Tuple[bytes, bytes]]:
        """Pares (id_antiguo, id_nuevo) tal como aparecen en HKLM\\SYSTEM\\MountedDevices."""
        plan, ss = self.plan, self.plan.sector_size
        disk_guid, signature, part_ids = plan.final_ids(identical)
        mapping = []
        for pp in plan.parts:
            if plan.source.style == "GPT":
                if not pp.src.unique_guid:
                    continue
                old = b"DMIO:ID:" + uuid.UUID(pp.src.unique_guid).bytes_le
                new = b"DMIO:ID:" + uuid.UUID(part_ids[pp.new_index]).bytes_le
            else:
                old = struct.pack("<IQ", plan.source.signature or 0, pp.src.start_lba * ss)
                new = struct.pack("<IQ", signature or 0, pp.new_start_lba * ss)
            if old != new:
                mapping.append((old, new))
        return mapping

    def _configure_boot(self):
        plan, opts, ss = self.plan, self.plan.options, self.plan.sector_size
        win = plan.windows_part()
        if win is None:
            self.log("No se detectó una instalación de Windows en el origen; no hay arranque que configurar")
            return
        identical = opts.id_mode == "same"
        mapping = self.mounted_devices_mapping(identical)
        if identical:
            if mapping:  # sólo MBR con particiones movidas
                self.platform.remap_mounted_devices(plan.target, win.new_start_lba * ss, mapping, self.log)
            self.log("Modo idéntico: se conserva la configuración de arranque (BCD) original")
            return
        if not opts.fix_boot:
            self.log("Reparación de arranque desactivada por el usuario", "warn")
            return
        sysp = plan.system_part()
        if sysp is None:
            self.log("No hay partición de sistema; no se puede reparar el arranque", "warn")
            return
        self.platform.make_bootable(plan.target, win.new_start_lba * ss,
                                    sysp.new_start_lba * ss, plan.firmware, self.log)
        if mapping:
            self.platform.remap_mounted_devices(plan.target, win.new_start_lba * ss, mapping, self.log)
