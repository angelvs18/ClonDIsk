"""Lectura y escritura del Master Boot Record (MBR clásico y protector)."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

MBR_SIG = b"\x55\xAA"
ENTRY_FMT = "<B3sB3sII"


class MbrError(Exception):
    pass


@dataclass
class MbrEntry:
    slot: int          # 0..3
    status: int        # 0x80 = activa
    ptype: int
    start_lba: int
    sectors: int

    @property
    def active(self) -> bool:
        return self.status == 0x80


def lba_to_chs(lba: int) -> bytes:
    heads, spt = 255, 63
    cyl = lba // (heads * spt)
    if cyl > 1023:
        return bytes((0xFE, 0xFF, 0xFF))
    head = (lba // spt) % heads
    sector = (lba % spt) + 1
    return bytes((head, ((cyl >> 2) & 0xC0) | sector, cyl & 0xFF))


def parse_mbr(sector0: bytes) -> Tuple[bytes, int, List[MbrEntry]]:
    """Devuelve (código_de_arranque[440], firma_de_disco, entradas_no_vacías)."""
    if len(sector0) < 512 or sector0[510:512] != MBR_SIG:
        raise MbrError("No hay una firma MBR válida (55AA)")
    boot_code = bytes(sector0[:440])
    signature = struct.unpack_from("<I", sector0, 440)[0]
    entries = []
    for i in range(4):
        status, _chs1, ptype, _chs2, start, count = struct.unpack_from(ENTRY_FMT, sector0, 446 + 16 * i)
        if ptype == 0 or count == 0:
            continue
        if status not in (0x00, 0x80):
            raise MbrError(f"Entrada MBR {i + 1} con estado inválido 0x{status:02X}")
        entries.append(MbrEntry(i, status, ptype, start, count))
    return boot_code, signature, entries


def is_protective(entries: List[MbrEntry]) -> bool:
    return any(e.ptype == 0xEE for e in entries)


def build_mbr(boot_code: bytes, signature: int, entries: List[MbrEntry],
              sector_size: int = 512) -> bytes:
    raw = bytearray(sector_size)
    code = (boot_code or b"")[:440]
    raw[0:len(code)] = code
    struct.pack_into("<I", raw, 440, signature & 0xFFFFFFFF)
    actives = [e for e in entries if e.active]
    if len(actives) > 1:
        raise MbrError("Solo puede haber una partición activa en MBR")
    for e in entries:
        if not 0 <= e.slot < 4:
            raise MbrError("MBR solo admite 4 entradas primarias")
        if e.start_lba + e.sectors > 0xFFFFFFFF + 1:
            raise MbrError("La partición excede el límite de 2 TiB de MBR")
        end = e.start_lba + e.sectors - 1
        struct.pack_into(ENTRY_FMT, raw, 446 + 16 * e.slot, e.status, lba_to_chs(e.start_lba),
                         e.ptype, lba_to_chs(end), e.start_lba, e.sectors)
    raw[510:512] = MBR_SIG
    return bytes(raw)


def build_protective_mbr(total_sectors: int, sector_size: int,
                         boot_code: Optional[bytes] = b"") -> bytes:
    count = min(total_sectors - 1, 0xFFFFFFFF)
    entry = MbrEntry(0, 0x00, 0xEE, 1, count)
    raw = bytearray(build_mbr(boot_code or b"", 0, [entry], sector_size))
    # El MBR protector usa CHS 0/0/2 de inicio según la especificación UEFI.
    raw[446 + 1:446 + 4] = bytes((0x00, 0x02, 0x00))
    return bytes(raw)
