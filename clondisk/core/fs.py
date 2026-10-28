"""Detección de sistemas de archivos y utilidades NTFS/FAT a bajo nivel.

* :func:`detect_fs` identifica el sistema de archivos por el sector de arranque.
* :func:`ntfs_used_ranges` lee el archivo ``$Bitmap`` de NTFS para copiar
  solo los clústeres en uso ("copia inteligente").
* :func:`patch_hidden_sectors` corrige el campo *hidden sectors* del sector
  de arranque cuando la partición cambia de posición; el código de arranque
  BIOS de NTFS/FAT lo usa para localizar BOOTMGR.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import List, Tuple

from .device import Device

Range = Tuple[int, int]  # (offset relativo a la partición, longitud) en bytes


class FsError(Exception):
    pass


def detect_fs(sector0: bytes) -> str:
    if len(sector0) < 512:
        return "?"
    oem = sector0[3:11]
    if oem == b"NTFS    ":
        return "NTFS"
    if oem == b"-FVE-FS-":
        return "BitLocker"
    if oem == b"EXFAT   ":
        return "exFAT"
    if sector0[3:7] == b"ReFS":
        return "ReFS"
    if sector0[510:512] == b"\x55\xAA":
        if sector0[0x52:0x5A] == b"FAT32   ":
            return "FAT32"
        if sector0[0x36:0x3B] == b"FAT16" or sector0[0x36:0x3B] == b"FAT12":
            return sector0[0x36:0x3B].decode()
    if all(b == 0 for b in sector0[:512]):
        return "Vacío"
    return "?"


# ---------------------------------------------------------------------------
# NTFS
# ---------------------------------------------------------------------------
@dataclass
class NtfsBoot:
    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_lcn: int
    mft_record_size: int

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def total_clusters(self) -> int:
        return self.total_sectors // self.sectors_per_cluster

    @property
    def volume_bytes(self) -> int:
        return self.total_sectors * self.bytes_per_sector


def parse_ntfs_boot(sector0: bytes) -> NtfsBoot:
    if sector0[3:11] != b"NTFS    ":
        raise FsError("No es un sector de arranque NTFS")
    bps = struct.unpack_from("<H", sector0, 0x0B)[0]
    spc_raw = sector0[0x0D]
    # Clústeres > 64 KiB (Windows 10 1709+) codifican spc como 2^(256-x).
    spc = spc_raw if spc_raw <= 0x80 else 1 << (256 - spc_raw)
    total = struct.unpack_from("<Q", sector0, 0x28)[0]
    mft_lcn = struct.unpack_from("<Q", sector0, 0x30)[0]
    cpr = struct.unpack_from("<b", sector0, 0x40)[0]
    if bps not in (512, 1024, 2048, 4096) or spc == 0:
        raise FsError("Geometría NTFS inválida")
    rec = (1 << -cpr) if cpr < 0 else cpr * bps * spc
    return NtfsBoot(bps, spc, total, mft_lcn, rec)


def _apply_fixups(rec: bytearray) -> bytearray:
    if rec[0:4] != b"FILE":
        raise FsError("Registro MFT sin firma FILE")
    usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
    usn = rec[usa_off:usa_off + 2]
    for i in range(1, usa_cnt):
        pos = i * 512 - 2
        if pos + 2 > len(rec):
            break
        if rec[pos:pos + 2] != usn:
            raise FsError("Fixup de registro MFT incorrecto (registro dañado)")
        rec[pos:pos + 2] = rec[usa_off + 2 * i: usa_off + 2 * i + 2]
    return rec


def _parse_runs(data: bytes, pos: int) -> List[Tuple[int, int]]:
    """Devuelve [(lcn, longitud_en_clústeres)]; lcn=None para runs dispersos."""
    runs = []
    lcn = 0
    while pos < len(data):
        header = data[pos]
        if header == 0:
            break
        len_sz, off_sz = header & 0x0F, header >> 4
        pos += 1
        length = int.from_bytes(data[pos:pos + len_sz], "little")
        pos += len_sz
        if off_sz:
            delta = int.from_bytes(data[pos:pos + off_sz], "little", signed=True)
            pos += off_sz
            lcn += delta
            runs.append((lcn, length))
        else:
            runs.append((None, length))
    return runs


def read_ntfs_bitmap(dev: Device, base: int, boot: NtfsBoot) -> bytes:
    """Lee el contenido de $Bitmap (registro MFT número 6)."""
    rec_off = base + boot.mft_lcn * boot.cluster_size + 6 * boot.mft_record_size
    rec = _apply_fixups(bytearray(dev.read_any(rec_off, boot.mft_record_size)))
    attr = struct.unpack_from("<H", rec, 0x14)[0]
    while attr + 8 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, attr)
        if atype == 0xFFFFFFFF or alen == 0:
            break
        if atype == 0x20:
            raise FsError("$Bitmap usa una lista de atributos (no soportado)")
        if atype == 0x80 and rec[attr + 9] == 0:  # $DATA sin nombre
            if rec[attr + 8] == 0:  # residente
                vlen, voff = struct.unpack_from("<IH", rec, attr + 0x10)
                return bytes(rec[attr + voff: attr + voff + vlen])
            runs_off = struct.unpack_from("<H", rec, attr + 0x20)[0]
            real_size = struct.unpack_from("<Q", rec, attr + 0x30)[0]
            runs = _parse_runs(bytes(rec[attr:attr + alen]), runs_off)
            out = bytearray()
            for lcn, length in runs:
                nbytes = length * boot.cluster_size
                if lcn is None:
                    out += b"\0" * nbytes
                else:
                    out += dev.read(base + lcn * boot.cluster_size, nbytes)
                if len(out) >= real_size:
                    break
            if len(out) < real_size:
                raise FsError("$Bitmap incompleto")
            return bytes(out[:real_size])
        attr += alen
    raise FsError("No se encontró el atributo $DATA de $Bitmap")


def merge_ranges(ranges: List[Range], gap: int = 0) -> List[Range]:
    out: List[List[int]] = []
    for off, ln in sorted(ranges):
        if ln <= 0:
            continue
        if out and off <= out[-1][0] + out[-1][1] + gap:
            end = max(out[-1][0] + out[-1][1], off + ln)
            out[-1][1] = end - out[-1][0]
        else:
            out.append([off, ln])
    return [(o, l) for o, l in out]


def ntfs_used_ranges(dev: Device, base: int, part_size: int,
                     merge_gap: int = 1024 * 1024) -> Tuple[List[Range], NtfsBoot]:
    """Calcula los rangos (relativos a la partición) que contienen datos.

    Incluye siempre el sector de arranque y su copia de respaldo al final del
    volumen. La granularidad es de 8 clústeres (un byte del bitmap) y los
    huecos menores a ``merge_gap`` se fusionan para reducir operaciones de E/S.
    """
    boot = parse_ntfs_boot(dev.read_any(base, 512))
    if boot.volume_bytes > part_size:
        raise FsError("El volumen NTFS es mayor que su partición")
    bitmap = read_ntfs_bitmap(dev, base, boot)
    nbytes_needed = -(-boot.total_clusters // 8)
    bitmap = bitmap[:nbytes_needed]
    cs = boot.cluster_size
    group = 8 * cs
    ranges: List[Range] = []
    for m in re.finditer(rb"[^\x00]+", bitmap):
        start = m.start() * group
        end = min(m.end() * group, boot.total_clusters * cs)
        if end > start:
            ranges.append((start, end - start))
    bps = boot.bytes_per_sector
    ranges.append((0, max(cs, 8192)))
    # Copia de respaldo del sector de arranque (último sector del volumen).
    backup = boot.total_sectors * bps
    if backup + bps <= part_size:
        ranges.append((backup, bps))
    ranges = merge_ranges(ranges, merge_gap)
    # Recortar al tamaño de la partición y alinear a sector del dispositivo.
    ss = dev.sector_size
    out = []
    for off, ln in ranges:
        o = (off // ss) * ss
        e = min(-(-(off + ln) // ss) * ss, (part_size // ss) * ss)
        if e > o:
            out.append((o, e - o))
    return merge_ranges(out), boot


def ntfs_used_bytes(dev: Device, base: int, part_size: int) -> int:
    boot = parse_ntfs_boot(dev.read_any(base, 512))
    bitmap = read_ntfs_bitmap(dev, base, boot)[: -(-boot.total_clusters // 8)]
    used = sum(bin(b).count("1") for b in bitmap) if len(bitmap) < 4096 else \
        int.from_bytes(bitmap, "little").bit_count()
    return min(used, boot.total_clusters) * boot.cluster_size


# ---------------------------------------------------------------------------
# Corrección de "hidden sectors" tras mover una partición
# ---------------------------------------------------------------------------
def patch_hidden_sectors(dev: Device, base: int, part_size: int, new_start_lba: int) -> List[str]:
    """Actualiza el campo hidden sectors (offset 0x1C) del sector de arranque
    de NTFS/FAT y sus copias de respaldo. ``base`` es el offset de la partición
    en ``dev``. Devuelve una lista de descripciones de lo modificado."""
    changes: List[str] = []
    ss = dev.sector_size
    first = bytearray(dev.read(base, ss))
    fs = detect_fs(bytes(first[:512]))
    if fs not in ("NTFS", "FAT32", "FAT16", "FAT12"):
        return changes
    value = new_start_lba & 0xFFFFFFFF

    def patch(offset: int, label: str):
        sec_base = (offset // ss) * ss
        buf = bytearray(dev.read(sec_base, ss))
        rel = offset - sec_base
        if detect_fs(bytes(buf[rel:rel + 512])) != fs:
            return
        old = struct.unpack_from("<I", buf, rel + 0x1C)[0]
        if old != value:
            struct.pack_into("<I", buf, rel + 0x1C, value)
            dev.write(sec_base, bytes(buf))
            changes.append(f"{label}: {old} → {value}")

    patch(base, f"{fs} sector de arranque")
    if fs == "NTFS":
        boot = parse_ntfs_boot(bytes(first[:512]))
        backup = boot.total_sectors * boot.bytes_per_sector
        if backup + boot.bytes_per_sector <= part_size:
            patch(base + backup, "NTFS copia de arranque")
    elif fs == "FAT32":
        bps = struct.unpack_from("<H", first, 0x0B)[0]
        bk = struct.unpack_from("<H", first, 0x32)[0]
        if 0 < bk < 32:
            patch(base + bk * bps, "FAT32 copia de arranque")
    return changes


def ntfs_label(dev: Device, base: int) -> str:
    """Etiqueta del volumen NTFS (atributo $VOLUME_NAME del registro MFT 3)."""
    boot = parse_ntfs_boot(dev.read_any(base, 512))
    off = base + boot.mft_lcn * boot.cluster_size + 3 * boot.mft_record_size
    rec = _apply_fixups(bytearray(dev.read_any(off, boot.mft_record_size)))
    attr = struct.unpack_from("<H", rec, 0x14)[0]
    while attr + 8 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, attr)
        if atype == 0xFFFFFFFF or alen == 0:
            break
        if atype == 0x60 and rec[attr + 8] == 0:
            vlen, voff = struct.unpack_from("<IH", rec, attr + 0x10)
            return bytes(rec[attr + voff: attr + voff + vlen]).decode("utf-16-le", "ignore")
        attr += alen
    return ""


def fat_label(sector0: bytes) -> str:
    raw = sector0[0x47:0x52] if sector0[0x52:0x5A] == b"FAT32   " else sector0[0x2B:0x36]
    label = raw.decode("ascii", "ignore").strip()
    return "" if label.upper() == "NO NAME" else label
