"""Lectura de la estructura de un disco directamente desde sus sectores."""
from __future__ import annotations

from .device import Device
from .fs import FsError, detect_fs, fat_label, ntfs_label, ntfs_used_bytes
from .gpt import GptError, parse_gpt
from .mbr import MbrError, is_protective, parse_mbr
from .model import Disk, Partition


def scan_disk(dev: Device, number: int, model: str = "", compute_usage: bool = True,
              size_bytes: int | None = None) -> Disk:
    ss = dev.sector_size
    size = size_bytes or dev.size
    disk = Disk(number=number, path=dev.path, model=model or dev.path,
                size_bytes=size, sector_size=ss)
    total = size // ss
    sector0 = dev.read(0, ss)
    try:
        boot_code, signature, mbr_entries = parse_mbr(sector0)
    except MbrError:
        disk.style = "RAW"
        return disk
    disk.boot_code = boot_code

    if is_protective(mbr_entries):
        try:
            table = parse_gpt(dev.read, ss, total)
        except GptError as e:
            disk.style = "RAW"
            disk.problems.append(f"GPT ilegible: {e}")
            return disk
        disk.style = "GPT"
        disk.disk_guid = table.disk_guid
        if table.used_backup:
            disk.problems.append("La GPT primaria está dañada; se usó la copia de respaldo")
        for e in table.entries:
            disk.partitions.append(Partition(
                index=e.slot + 1, start_lba=e.first_lba, sectors=e.sectors,
                type_guid=e.type_guid, unique_guid=e.unique_guid,
                attributes=e.attributes, name=e.name))
    else:
        disk.style = "MBR"
        disk.signature = signature
        for e in mbr_entries:
            disk.partitions.append(Partition(
                index=e.slot + 1, start_lba=e.start_lba, sectors=e.sectors,
                mbr_type=e.ptype, active=e.active))
            if e.active:
                disk.partitions[-1].is_system = True

    disk.partitions.sort(key=lambda p: p.start_lba)
    for p in disk.partitions:
        if p.role in ("extended", "ldm", "spaces"):
            p.fs = {"extended": "Extendida", "ldm": "LDM", "spaces": "Spaces"}[p.role]
            continue
        if p.role == "msr":
            p.fs = "MSR"
            continue
        if p.end_lba > total:
            p.fs = "?"
            disk.problems.append(f"La partición {p.index} sobrepasa el final del disco")
            continue
        try:
            sector = dev.read_any(p.offset_bytes(ss), 512)
            p.fs = detect_fs(sector)
            if p.fs == "NTFS":
                p.label = ntfs_label(dev, p.offset_bytes(ss))
            elif p.fs.startswith("FAT"):
                p.label = fat_label(sector)
        except Exception:
            p.fs = p.fs if p.fs != "?" else "?"
        if p.role == "efi" and p.fs.startswith("FAT"):
            p.is_system = True
        if compute_usage and p.fs == "NTFS":
            try:
                p.used_bytes = ntfs_used_bytes(dev, p.offset_bytes(ss), p.size_bytes(ss))
            except (FsError, Exception):
                p.used_bytes = None
    return disk
