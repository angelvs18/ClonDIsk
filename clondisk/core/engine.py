"""Orquestador de la clonación.

Secuencia (modo «nuevos identificadores»):

1. Validar el plan y preparar el destino (offline, escribible).
2. Crear instantáneas VSS de los volúmenes montados del origen.
3. Calcular qué rangos copiar (bitmap NTFS o sector a sector).
4. Borrar las tablas antiguas del destino y copiar cada partición a su nueva posición.
5. Verificar (opcional) y corregir «hidden sectors» de particiones movidas.
6. Escribir la tabla de particiones (al final: si se cancela, el destino queda vacío).
7. Conectar el destino, ampliar NTFS en las particiones agrandadas.
8. Reconstruir el arranque (bcdboot) y actualizar MountedDevices del Windows clonado;
   opcionalmente, habilitar los drivers de almacenamiento para otra computadora.
9. Convertir a GPT/UEFI con mbr2gpt (opcional, solo origen MBR).
10. Comprobar que el destino puede arrancar (MBR/EFI, BCD, hidden sectors).
11. TRIM del SSD.

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
from .device import Device, is_access_denied
from .fs import FsError, ntfs_used_ranges, patch_hidden_sectors
from .gpt import GptEntry, build_gpt
from .mbr import MbrEntry, build_mbr
from .model import MiB, fmt_size
from .planner import ClonePlan, PlannedPartition, estimate_minutes, fmt_minutes_range
from .scan import scan_disk
from .winprep import (PENDING_UPDATES_WARNING, SECURE_BOOT_2023_WARNING, WINRE_ENABLE_HINT,
                      WinreStatus, assess_health, classify_boot_signature, winre_check)

BootCheck = Tuple[bool, str]
MAX_BAD_SECTOR_LOG = 50
VSS_MIN_BYTES = 300 * MiB     # «Reservado para el sistema» y similares: se leen directamente
SPEED_SAMPLE_BYTES = 256 * MiB  # lectura secuencial para medir la velocidad del origen


class TargetWriteDenied(RuntimeError):
    pass

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
    "Convertir a GPT/UEFI",
    "Comprobar arranque",
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
    bad_sectors: List[int] = field(default_factory=list)   # LBA del origen copiados con ceros
    boot_checks: List[BootCheck] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)       # avisos para la pantalla final
    extra_checks: List[BootCheck] = field(default_factory=list)  # ✓/✗ que no impiden arrancar

    @property
    def boot_ok(self) -> bool:
        return all(ok for ok, _ in self.boot_checks)


def fmt_lbas(lbas: List[int]) -> str:
    """Agrupa LBA consecutivos: ``100-107, 2048``."""
    out: List[str] = []
    start = prev = None
    for lba in sorted(lbas):
        if start is not None and lba == prev + 1:
            prev = lba
            continue
        if start is not None:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = lba
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


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
        self.bad_sectors: List[int] = []
        self.boot_checks: List[BootCheck] = []
        self.notices: List[str] = []
        self.extra_checks: List[BootCheck] = []
        self._target_touched = False
        self._efi_offset: Optional[int] = None    # partición EFI del destino (tras comprobar)

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
            self._log_source_health()
            self._check_pending_updates()

            self.step(1)
            if not dry:
                self._target_touched = True
                self.platform.prepare_target(plan.target, self.log)
                self._probe_target_write()   # antes de crear instantáneas
            self._check_cancel()

            self.step(2)
            if opts.use_vss and self.platform.supports_vss and not dry:
                for pp in plan.parts:
                    p = pp.src
                    # Solo volúmenes con letra: son los que pueden estar en uso.
                    # Recuperación/EFI no montados se leen directamente del disco.
                    if p.fs != "NTFS" or not p.letter:
                        continue
                    if p.size_bytes(plan.sector_size) < VSS_MIN_BYTES:
                        self.log(f"{p.display_name()} ({fmt_size(p.size_bytes(plan.sector_size))}) "
                                 "se leerá directamente del disco, sin instantánea")
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
            speed = self._measure_read_speed(src)
            lo, hi = estimate_minutes(copy_total, opts.verify, speed)
            self.log(f"Tiempo estimado: {fmt_minutes_range(lo, hi)}"
                     + (" (incluye verificar)" if opts.verify else ""))
            if dry:
                return self._finish(True, "Simulación completada: el plan es válido. "
                                    f"Se copiarían {fmt_size(copy_total)}.")

            dst = self.platform.open_disk(plan.target, write=True)
            if dst.size < plan.target.size_bytes:
                raise RuntimeError("El tamaño real del destino es menor que el informado")

            self.step(4)
            self._wipe_tables(dst)
            copier = Copier(self.cancel, self._on_bytes, skip_bad_sectors=opts.skip_bad_sectors,
                            on_bad_sector=self._on_bad_sector)
            self._copy_t0 = time.monotonic()
            self._progress(0, self.total, 0.0)
            ss = plan.sector_size
            for pp in plan.parts:
                sdev, sbase = self._source_for(pp, src)
                ranges = self.ranges[pp.new_index]
                self.log(f"Copiando {pp.src.display_name()} ({pp.copy_mode == 'smart' and 'inteligente' or 'sector a sector'}, "
                         f"{fmt_size(sum(l for _, l in ranges))}) → offset {fmt_size(pp.new_start_lba * ss)}")
                copier.copy(sdev, sbase, dst, pp.new_start_lba * ss, ranges, lba_base=pp.src.start_lba)
            dst.flush()
            if self.bad_sectors:
                self.log(f"{len(self.bad_sectors)} sector(es) ilegibles copiados con ceros: "
                         + fmt_lbas(self.bad_sectors), "warn")

            self.step(5)
            if opts.verify:
                for pp in plan.parts:
                    sdev, sbase = self._source_for(pp, src)
                    copier.verify(sdev, sbase, dst, pp.new_start_lba * ss, self.ranges[pp.new_index],
                                  lba_base=pp.src.start_lba)
                    self.log(f"Verificada {pp.src.display_name()} — OK")
            else:
                self.log("Verificación desactivada")

            self.step(6)
            for pp in plan.parts:
                if pp.moved:
                    changes = patch_hidden_sectors(dst, pp.new_start_lba * ss,
                                                   pp.new_sectors * ss, pp.new_start_lba)
                    for c in changes:
                        self.log(f"{pp.src.display_name()} — {c}")
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
            if plan.converts_to_gpt:
                self._convert_to_gpt()
            else:
                self.log("Sin conversión: el destino conserva el estilo " + plan.source.style)

            self.step(10)
            self._register_winre()
            self._verify_boot()
            self._check_boot_signature()

            self.step(11)
            if opts.retrim:
                try:
                    self.platform.retrim(plan.target, self.log)
                except Exception as e:
                    self.log(f"TRIM no disponible: {e}", "warn")
            self._release_letters()
            if identical:
                self.platform.set_offline(plan.target, self.log)
                dst = self.platform.open_disk(plan.target, write=True)
                self._write_table(dst, plan.final_ids(identical=True))
                dst.flush()
                dst.close()
                dst = None
                self.log("Identificadores originales restaurados en el destino; el disco "
                         "queda offline. Retira el disco de origen antes de arrancar.", "warn")
            msg = "Clonación completada correctamente"
            if not all(ok for ok, _ in self.boot_checks):
                msg = ("Clonación completada, pero la comprobación del arranque encontró "
                       "problemas (✗). Revísalos antes de instalar el SSD.")
                self.log(msg, "warn")
            if self.bad_sectors:
                msg += f" · {len(self.bad_sectors)} sector(es) ilegibles se copiaron con ceros."
            return self._finish(True, msg)
        except CancelledError as e:
            self.log(str(e), "warn")
            return self._finish(False, "Clonación cancelada. El destino no tiene una tabla "
                                "de particiones válida; puedes volver a intentarlo.", cancelled=True)
        except TargetWriteDenied as e:
            self.log(f"ERROR: {e}", "error")
            return self._finish(False, str(e))
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
            if self.snapshots:
                self.log(f"Liberando {len(self.snapshots)} instantánea(s) VSS de esta clonación")
            self._release_snapshots()
            if self._target_touched:
                self._release_letters()

    def _release_letters(self):
        """El destino no debe quedar con letras asignadas por ClonDisk."""
        try:
            left = self.platform.release_target_letters(self.plan.target, self.log)
        except Exception as e:
            self.log(f"No se pudieron quitar las letras temporales del destino: {e}", "warn")
            return
        if left:
            self.log("Quedaron letras asignadas por ClonDisk en el destino: "
                     + ", ".join(f"{l}:" for l in left) + ". Quítalas en Administración de "
                     "discos o con «Preparar para desconectar el disco destino».", "warn")

    # ------------------------------------------------------------------
    def _probe_target_write(self):
        """Lee el sector 0 del destino y lo reescribe igual: detecta un «acceso denegado»
        (antivirus, Acceso controlado a carpetas) antes de crear instantáneas o copiar."""
        plan = self.plan
        dev = None
        try:
            dev = self.platform.open_disk(plan.target, write=True)
            data = dev.read(0, plan.sector_size)
            dev.write(0, data)
            dev.flush()
            self.log("Escritura de prueba en el destino: OK")
        except OSError as e:
            if not is_access_denied(e):
                raise
            try:
                advice = self.platform.write_denied_diagnosis(plan.target)
            except Exception:
                advice = ""
            raise TargetWriteDenied(
                f"Windows denegó la escritura en el Disco {plan.target.number} ({e}). No se "
                "crearon instantáneas ni se copió nada; el origen está intacto.\n\n" + advice) from None
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass

    def _finish(self, ok: bool, msg: str, cancelled: bool = False) -> CloneResult:
        elapsed = time.monotonic() - self._t0
        self.log(msg, "ok" if ok else ("warn" if cancelled else "error"))
        return CloneResult(ok, msg, cancelled, elapsed, self.done, list(self.warnings),
                           list(self.bad_sectors), list(self.boot_checks), list(self.notices),
                           list(self.extra_checks))

    def _on_bad_sector(self, lba: int):
        self.bad_sectors.append(lba)
        n = len(self.bad_sectors)
        if n <= MAX_BAD_SECTOR_LOG:
            self.log(f"Sector ilegible en el origen, LBA {lba}: se escribieron ceros", "warn")
        elif n == MAX_BAD_SECTOR_LOG + 1:
            self.log("Hay más sectores ilegibles; la lista completa aparece al final de la copia", "warn")

    def _log_source_health(self):
        try:
            health = self.platform.disk_health(self.plan.source)
        except Exception as e:
            self.log(f"No se pudo consultar la salud del origen: {e}", "warn")
            return
        level, msgs = assess_health(health)
        for m in msgs:
            self.log(f"Salud del origen: {m}", "warn" if level == "danger" else "info")
        if level == "danger":
            self.log("El disco de origen muestra signos de fallo: considera activar «Saltar "
                     "sectores ilegibles» y «Verificar», y respalda primero lo más importante.", "warn")

    def _check_pending_updates(self):
        win = self.plan.windows_part()
        if win is None:
            return
        try:
            reasons = self.platform.pending_updates(win.src)
        except Exception as e:
            self.log(f"No se pudo comprobar si el Windows de origen tiene actualizaciones "
                     f"pendientes: {e}")
            return
        if reasons is None:
            return
        if not reasons:
            self.log("El Windows de origen no tiene actualizaciones pendientes")
            return
        self.log("Actualizaciones pendientes en el Windows de origen: " + "; ".join(reasons), "warn")
        self.notices.append(PENDING_UPDATES_WARNING)

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
                    self.log(f"{pp.src.display_name()} — NTFS, clúster {boot.cluster_size} B, "
                             f"{fmt_size(used)} en uso de {fmt_size(size)} ({len(ranges)} bloques)")
                    continue
                except Exception as e:
                    self.log(f"{pp.src.display_name()} — no se pudo leer el bitmap NTFS ({e}); "
                             "se copiará sector a sector", "warn")
                    pp.copy_mode = "raw"
            self.ranges[pp.new_index] = [(0, size)]
            self.log(f"{pp.src.display_name()} — {pp.src.fs}, copia sector a sector de {fmt_size(size)}")

    def _measure_read_speed(self, src: Device) -> Optional[float]:
        """Lectura secuencial corta (~256 MB) del origen para estimar el tiempo real."""
        plan, ss = self.plan, self.plan.sector_size
        pp = plan.windows_part() or max(plan.parts, key=lambda x: x.src.sectors, default=None)
        if pp is None:
            return None
        off = pp.src.start_lba * ss
        length = min(SPEED_SAMPLE_BYTES, pp.copy_sectors() * ss)
        done, chunk = 0, 8 * MiB
        t0 = time.monotonic()
        try:
            while done < length:
                self._check_cancel()
                n = min(chunk, length - done)
                src.read(off + done, n)
                done += n
        except CancelledError:
            raise
        except Exception as e:
            self.log(f"No se pudo medir la velocidad de lectura del origen ({e}); "
                     "el tiempo estimado es aproximado")
            return None
        dt = time.monotonic() - t0
        if done <= 0 or dt <= 0:
            return None
        speed = done / dt
        self.log(f"Velocidad de lectura del origen: {fmt_size(speed)}/s "
                 f"(lectura secuencial de {fmt_size(done)})")
        return speed

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
        sysp = plan.system_part()
        if identical:
            if mapping:  # sólo MBR con particiones movidas
                self.platform.remap_mounted_devices(plan.target, win.new_start_lba * ss, mapping, self.log)
            self.log("Modo idéntico: se conserva la configuración de arranque (BCD) original")
        elif not opts.fix_boot:
            self.log("Reparación de arranque desactivada por el usuario", "warn")
        elif sysp is None:
            self.log("No hay partición de sistema; no se puede reparar el arranque", "warn")
        else:
            info = win.src.win_info
            self.platform.make_bootable(plan.target, win.new_start_lba * ss,
                                        sysp.new_start_lba * ss, plan.firmware, self.log,
                                        win_build=info.build if info else None)
            if mapping:
                self.platform.remap_mounted_devices(plan.target, win.new_start_lba * ss, mapping, self.log)
        if opts.new_hardware:
            self.platform.prepare_new_hardware(plan.target, win.new_start_lba * ss, self.log)
            self.log("Windows preparado para otra computadora. Es posible que Windows y el "
                     "software con licencia (p. ej. el del plotter) pidan reactivación por el "
                     "cambio de hardware.", "warn")

    # ------------------------------------------------------------------
    def _convert_to_gpt(self):
        """mbr2gpt sobre el destino y MountedDevices de MBR (firma+offset) a GPT (GUID)."""
        plan, ss = self.plan, self.plan.sector_size
        try:
            self.platform.convert_to_gpt(plan.target, self.log)
        except Exception as e:
            self.log(f"La conversión a GPT/UEFI falló: {e}", "warn")
            self.log("El destino sigue siendo MBR y arrancará solo en modo BIOS/Legacy (CSM)", "warn")
            self.boot_checks.append((False, "Conversión a GPT/UEFI con mbr2gpt"))
            return
        self.boot_checks.append((True, "Conversión a GPT/UEFI con mbr2gpt"))
        win = plan.windows_part()
        if win is None:
            return
        with self.platform.open_disk(plan.target, write=False) as dev:
            disk = scan_disk(dev, plan.target.number, plan.target.model, compute_usage=False)
        by_start = {p.start_lba: p for p in disk.partitions}
        mapping = []
        for pp in plan.parts:
            g = by_start.get(pp.new_start_lba)
            if g is not None and g.unique_guid:
                mapping.append((struct.pack("<IQ", plan.new_signature or 0, pp.new_start_lba * ss),
                                b"DMIO:ID:" + uuid.UUID(g.unique_guid).bytes_le))
        if mapping:
            self.platform.remap_mounted_devices(plan.target, win.new_start_lba * ss, mapping, self.log)

    def _register_winre(self):
        """Localiza Winre.wim en el destino y registra su ubicación sin conexión. WinRE no se
        puede habilitar sin conexión: se indica ejecutar «reagentc /enable» al primer arranque."""
        plan, ss = self.plan, self.plan.sector_size
        win = plan.windows_part()
        if win is None:
            return
        if plan.options.id_mode == "same":
            self.log("Modo idéntico: WinRE conserva la configuración del origen")
            return
        with self.platform.open_disk(plan.target, write=False) as dev:
            disk = scan_disk(dev, plan.target.number, plan.target.model, compute_usage=False)
        rec = next((p for p in disk.partitions if p.role == "recovery"), None)
        try:
            st = self.platform.register_winre(plan.target, win.new_start_lba * ss,
                                              rec.start_lba * ss if rec else None, self.log)
        except Exception as e:
            st = WinreStatus(detail=str(e))
        if st is None:
            return
        ok, text = winre_check(st)
        self.extra_checks.append((ok, text))
        self.log(("✓ " if ok else "✗ ") + text, "ok" if ok else "warn")
        if not st.enabled:
            self.notices.append(WINRE_ENABLE_HINT)

    def _check_boot_signature(self):
        """Secure Boot: ¿bootmgfw.efi está firmado con «Windows UEFI CA 2023»? Los firmwares
        anteriores a 2023 no tienen ese certificado y muestran «firma no válida»."""
        if self._efi_offset is None:
            return
        try:
            sig = self.platform.boot_signature(self.plan.target, self._efi_offset, self.log)
        except Exception as e:
            self.log(f"No se pudo leer la firma de bootmgfw.efi: {e}", "warn")
            return
        if not sig:
            return
        chain = [str(c) for c in (sig.get("Chain") or [])]
        kind = classify_boot_signature(sig.get("Issuer"), chain)
        self.log(f"Firma de \\EFI\\Microsoft\\Boot\\bootmgfw.efi: {sig.get('Status') or '?'}; "
                 f"firmante: {sig.get('Subject') or '?'}; emisor: {sig.get('Issuer') or '?'}"
                 + (f"; cadena: {' → '.join(chain)}" if chain else ""))
        if kind == "2023":
            self.log(SECURE_BOOT_2023_WARNING, "warn")
            self.notices.append(SECURE_BOOT_2023_WARNING)
        elif kind == "2011":
            self.log("bootmgfw.efi está firmado con «Microsoft Windows Production PCA 2011»: "
                     "lo aceptan también los firmwares anteriores a 2023")
        else:
            self.log("No se reconoció el certificado de bootmgfw.efi"
                     + (" (no está firmado)" if kind == "unsigned" else ""), "warn")

    def _verify_boot(self):
        """Comprueba que el destino puede arrancar. Los fallos son advertencias visibles."""
        plan, opts, ss = self.plan, self.plan.options, self.plan.sector_size
        win = plan.windows_part()
        if win is None:
            self.log("No hay Windows en el destino: no se comprueba el arranque")
            return
        checks: List[BootCheck] = []
        names = {pp.new_start_lba: pp.src.display_name() for pp in plan.parts}
        sys_off = None
        with self.platform.open_disk(plan.target, write=False) as dev:
            disk = scan_disk(dev, plan.target.number, plan.target.model, compute_usage=False)
            sector0 = dev.read(0, ss)
            if disk.style == "MBR":
                checks.append((sector0[510:512] == b"\x55\xAA", "MBR con firma 55AA"))
                code = (plan.source.boot_code or b"")[:440].ljust(440, b"\0")
                checks.append((sector0[:440] == code, "Código de arranque del MBR copiado del origen"))
                sysp = plan.system_part()
                actives = [p for p in disk.partitions if p.active]
                ok = (len(actives) == 1 and sysp is not None
                      and actives[0].start_lba == sysp.new_start_lba)
                label = names.get(sysp.new_start_lba, "sistema") if sysp else "sistema"
                checks.append((ok, f"Una sola partición activa y es la de sistema ({label})"
                               + ("" if ok else f" — activas encontradas: {len(actives)}")))
                for p in disk.partitions:
                    if p.fs not in ("NTFS", "FAT32", "FAT16", "FAT12"):
                        continue
                    hs = struct.unpack_from("<I", dev.read(p.start_lba * ss, ss), 0x1C)[0]
                    want = p.start_lba & 0xFFFFFFFF
                    checks.append((hs == want, f"Hidden sectors de {names.get(p.start_lba, p.index)}: "
                                               f"{hs}" + ("" if hs == want else f" (esperado {want})")))
                if sysp is not None:
                    sys_off = sysp.new_start_lba * ss
            elif disk.style == "GPT":
                efi = [p for p in disk.partitions if p.role == "efi"]
                checks.append((bool(efi), "Partición del sistema EFI presente"))
                if efi:
                    sys_off = self._efi_offset = efi[0].start_lba * ss
            else:
                checks.append((False, "El destino no tiene una tabla de particiones válida"))
        if sys_off is not None:
            try:
                # En modo idéntico el BCD apunta a los IDs originales, que se escriben al final.
                checks += self.platform.boot_file_checks(plan.target, disk.style, sys_off,
                                                         win.new_start_lba * ss, self.log,
                                                         check_bcd=opts.id_mode != "same")
            except Exception as e:
                checks.append((False, f"No se pudieron revisar los archivos de arranque: {e}"))
        self.boot_checks += checks
        for ok, text in checks:
            self.log(("✓ " if ok else "✗ ") + text, "ok" if ok else "warn")
