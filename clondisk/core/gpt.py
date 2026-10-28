"""Lectura y escritura de tablas de particiones GPT (UEFI 2.x).

Todo el código trabaja con bytes en memoria; quien llama decide dónde
escribir los bloques devueltos por :func:`build_gpt`.
"""
from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .mbr import build_protective_mbr

GPT_SIGNATURE = b"EFI PART"
GPT_REVISION = 0x00010000
GPT_HEADER_SIZE = 92
HEADER_FMT = "<8sIIIIQQQQ16sQIII"
ENTRY_FMT = "<16s16sQQQ72s"
ENTRY_SIZE = 128
NUM_ENTRIES = 128

assert struct.calcsize(HEADER_FMT) == GPT_HEADER_SIZE
assert struct.calcsize(ENTRY_FMT) == ENTRY_SIZE

ReadFn = Callable[[int, int], bytes]


class GptError(Exception):
    pass


def guid_to_bytes(guid: str) -> bytes:
    return uuid.UUID(guid).bytes_le


def bytes_to_guid(raw: bytes) -> str:
    return str(uuid.UUID(bytes_le=bytes(raw))).upper()


def new_guid() -> str:
    return str(uuid.uuid4()).upper()


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class GptEntry:
    slot: int                 # posición 0-based en el arreglo de entradas
    type_guid: str
    unique_guid: str
    first_lba: int
    last_lba: int             # inclusivo, como en la especificación
    attributes: int = 0
    name: str = ""

    @property
    def sectors(self) -> int:
        return self.last_lba - self.first_lba + 1


@dataclass
class GptTable:
    disk_guid: str
    first_usable: int
    last_usable: int
    entries: List[GptEntry] = field(default_factory=list)
    num_entries: int = NUM_ENTRIES
    entry_size: int = ENTRY_SIZE
    used_backup: bool = False


def entries_sectors(sector_size: int, num_entries: int = NUM_ENTRIES,
                    entry_size: int = ENTRY_SIZE) -> int:
    return -(-num_entries * entry_size // sector_size)


def usable_range(total_sectors: int, sector_size: int) -> Tuple[int, int]:
    """(first_usable, last_usable) para un disco nuevo con 128 entradas."""
    es = entries_sectors(sector_size)
    return 2 + es, total_sectors - 2 - es


def _parse_header(raw: bytes) -> Optional[dict]:
    if raw[:8] != GPT_SIGNATURE:
        return None
    (sig, rev, hsize, hcrc, _res, my_lba, alt_lba, first, last, dguid,
     ent_lba, n_ent, ent_size, ent_crc) = struct.unpack_from(HEADER_FMT, raw)
    if hsize < GPT_HEADER_SIZE or hsize > len(raw):
        return None
    tmp = bytearray(raw[:hsize])
    tmp[16:20] = b"\0\0\0\0"
    if crc32(bytes(tmp)) != hcrc:
        return None
    return dict(my_lba=my_lba, alt_lba=alt_lba, first=first, last=last,
                disk_guid=bytes_to_guid(dguid), entries_lba=ent_lba,
                num_entries=n_ent, entry_size=ent_size, entries_crc=ent_crc)


def parse_gpt(read: ReadFn, sector_size: int, total_sectors: int) -> GptTable:
    """Lee la GPT primaria (o la de respaldo si la primaria está dañada)."""
    candidates = [(1, False), (total_sectors - 1, True)]
    last_error = "No se encontró una cabecera GPT válida"
    for lba, is_backup in candidates:
        hdr = _parse_header(read(lba * sector_size, sector_size))
        if not hdr:
            continue
        if hdr["entry_size"] < ENTRY_SIZE or hdr["num_entries"] > 4096:
            last_error = "Cabecera GPT con tamaños no soportados"
            continue
        length = hdr["num_entries"] * hdr["entry_size"]
        n_sec = -(-length // sector_size)
        raw = read(hdr["entries_lba"] * sector_size, n_sec * sector_size)[:length]
        if crc32(raw) != hdr["entries_crc"]:
            last_error = "CRC del arreglo de entradas GPT incorrecto"
            continue
        entries = []
        for i in range(hdr["num_entries"]):
            chunk = raw[i * hdr["entry_size"]: i * hdr["entry_size"] + ENTRY_SIZE]
            t, u, first, last, attrs, name = struct.unpack(ENTRY_FMT, chunk)
            if t == b"\0" * 16:
                continue
            entries.append(GptEntry(
                slot=i, type_guid=bytes_to_guid(t), unique_guid=bytes_to_guid(u),
                first_lba=first, last_lba=last, attributes=attrs,
                name=name.decode("utf-16-le", "ignore").split("\0", 1)[0]))
        entries.sort(key=lambda e: e.first_lba)
        return GptTable(disk_guid=hdr["disk_guid"], first_usable=hdr["first"],
                        last_usable=hdr["last"], entries=entries,
                        num_entries=hdr["num_entries"], entry_size=hdr["entry_size"],
                        used_backup=is_backup)
    raise GptError(last_error)


def _pack_entry(e: Optional[GptEntry]) -> bytes:
    if e is None:
        return b"\0" * ENTRY_SIZE
    name = e.name.encode("utf-16-le")[:72].ljust(72, b"\0")
    return struct.pack(ENTRY_FMT, guid_to_bytes(e.type_guid), guid_to_bytes(e.unique_guid),
                       e.first_lba, e.last_lba, e.attributes, name)


def _pack_header(my_lba, alt_lba, first, last, disk_guid, entries_lba, entries_crc,
                 sector_size) -> bytes:
    fields = [GPT_SIGNATURE, GPT_REVISION, GPT_HEADER_SIZE, 0, 0, my_lba, alt_lba,
              first, last, guid_to_bytes(disk_guid), entries_lba, NUM_ENTRIES,
              ENTRY_SIZE, entries_crc]
    raw = struct.pack(HEADER_FMT, *fields)
    fields[3] = crc32(raw)
    raw = struct.pack(HEADER_FMT, *fields)
    return raw.ljust(sector_size, b"\0")


def build_gpt(disk_guid: str, entries: List[GptEntry], total_sectors: int,
              sector_size: int, boot_code: bytes = b"") -> List[Tuple[int, bytes]]:
    """Construye MBR protector + GPT primaria + GPT de respaldo.

    Devuelve una lista de ``(offset_en_bytes, datos)`` alineados a sector.
    """
    first, last = usable_range(total_sectors, sector_size)
    slots: List[Optional[GptEntry]] = [None] * NUM_ENTRIES
    for e in entries:
        if not 0 <= e.slot < NUM_ENTRIES:
            raise GptError(f"Ranura de entrada GPT inválida: {e.slot}")
        if e.first_lba < first or e.last_lba > last or e.last_lba < e.first_lba:
            raise GptError(
                f"La partición en ranura {e.slot + 1} ({e.first_lba}-{e.last_lba}) "
                f"queda fuera del área utilizable ({first}-{last})")
        slots[e.slot] = e
    ordered = sorted(entries, key=lambda e: e.first_lba)
    for a, b in zip(ordered, ordered[1:]):
        if b.first_lba <= a.last_lba:
            raise GptError("Hay particiones superpuestas en la tabla GPT")

    array = b"".join(_pack_entry(e) for e in slots)
    n_sec = entries_sectors(sector_size)
    array_padded = array.ljust(n_sec * sector_size, b"\0")
    ecrc = crc32(array)
    last_lba = total_sectors - 1
    backup_entries_lba = last_lba - n_sec

    primary = _pack_header(1, last_lba, first, last, disk_guid, 2, ecrc, sector_size)
    backup = _pack_header(last_lba, 1, first, last, disk_guid, backup_entries_lba, ecrc,
                          sector_size)
    pmbr = build_protective_mbr(total_sectors, sector_size, boot_code)
    return [
        (0, pmbr),
        (1 * sector_size, primary),
        (2 * sector_size, array_padded),
        (backup_entries_lba * sector_size, array_padded),
        (last_lba * sector_size, backup),
    ]
