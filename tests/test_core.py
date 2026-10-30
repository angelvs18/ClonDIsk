import struct
import subprocess
import threading

import pytest

from clondisk.core.device import FileDevice
from clondisk.core.engine import CloneEngine
from clondisk.core.fs import ntfs_used_ranges, parse_ntfs_boot
from clondisk.core.gpt import GptEntry, build_gpt, new_guid, parse_gpt
from clondisk.core.mbr import MbrEntry, build_mbr, parse_mbr
from clondisk.core.model import GUID_BASIC, GUID_EFI, MiB
from clondisk.core.planner import CloneOptions, build_plan
from clondisk.core.scan import scan_disk
from clondisk.platforms.images import ImagePlatform

from conftest import extract, make_blank, needs_tools, run


class MemDev:
    def __init__(self, size, ss=512):
        self.buf = bytearray(size)
        self.size = size
        self.sector_size = ss
        self.path = "mem"

    def read(self, off, n):
        return bytes(self.buf[off:off + n])

    def write(self, off, data):
        self.buf[off:off + len(data)] = data


# ---------------------------------------------------------------- GPT / MBR
@pytest.mark.parametrize("ss", [512, 4096])
def test_gpt_roundtrip(ss):
    total = (256 * MiB) // ss
    align = MiB // ss
    entries = [
        GptEntry(0, GUID_EFI, new_guid(), align, align + (100 * MiB) // ss - 1, 0, "EFI system partition"),
        GptEntry(1, GUID_BASIC, new_guid(), align * 101, align * 200 - 1, 0x8000000000000001, "Datos ñ"),
    ]
    dguid = new_guid()
    dev = MemDev(total * ss, ss)
    for off, data in build_gpt(dguid, entries, total, ss):
        assert off % ss == 0 and len(data) % ss == 0
        dev.write(off, data)
    table = parse_gpt(dev.read, ss, total)
    assert table.disk_guid == dguid
    assert [(e.unique_guid, e.first_lba, e.last_lba, e.attributes, e.name) for e in table.entries] == \
           [(e.unique_guid, e.first_lba, e.last_lba, e.attributes, e.name) for e in entries]
    # Corromper la primaria -> debe usar la de respaldo
    dev.write(ss, b"\0" * ss)
    t2 = parse_gpt(dev.read, ss, total)
    assert t2.used_backup and len(t2.entries) == 2


@needs_tools
def test_gpt_built_is_valid_for_sgdisk(tmp_path):
    img = str(tmp_path / "x.img")
    make_blank(img, 64)
    total = 64 * MiB // 512
    entries = [GptEntry(0, GUID_EFI, new_guid(), 2048, 2048 + 20479, 0, "EFI"),
               GptEntry(1, GUID_BASIC, new_guid(), 22528, total - 34 - 1, 0, "C")]
    with FileDevice(img, writable=True) as d:
        for off, data in build_gpt(new_guid(), entries, total, 512):
            d.write(off, data)
    out = run("sgdisk", "-v", img).stdout
    assert "No problems found" in out


def test_mbr_roundtrip():
    entries = [MbrEntry(0, 0x80, 0x07, 2048, 100000), MbrEntry(1, 0, 0x07, 104448, 500000)]
    raw = build_mbr(b"\x33\xC0" * 10, 0xDEADBEEF, entries)
    code, sig, parsed = parse_mbr(raw)
    assert sig == 0xDEADBEEF and code.startswith(b"\x33\xC0")
    assert [(e.slot, e.status, e.ptype, e.start_lba, e.sectors) for e in parsed] == \
           [(e.slot, e.status, e.ptype, e.start_lba, e.sectors) for e in entries]


# ---------------------------------------------------------------- escaneo
@needs_tools
def test_scan_gpt(gpt_source):
    img, parts, files = gpt_source
    with FileDevice(img) as d:
        disk = scan_disk(d, 0, "src")
    assert disk.style == "GPT"
    roles = [p.role for p in disk.partitions]
    assert roles == ["efi", "msr", "data", "recovery"]
    fss = [p.fs for p in disk.partitions]
    assert fss == ["FAT32", "MSR", "NTFS", "NTFS"]
    c = disk.partitions[2]
    assert c.used_bytes is not None
    assert c.used_bytes > sum(len(v) for v in files.values())


@needs_tools
def test_ntfs_ranges_cover_used(gpt_source):
    img, parts, files = gpt_source
    with FileDevice(img) as d:
        disk = scan_disk(d, 0, "src")
        c = disk.partitions[2]
        ranges, boot = ntfs_used_ranges(d, c.offset_bytes(512), c.size_bytes(512))
    copied = sum(l for _, l in ranges)
    assert copied < c.size_bytes(512) * 0.6  # copia inteligente realmente ahorra
    assert ranges[0][0] == 0
    last = ranges[-1]
    assert last[0] + last[1] == boot.total_sectors * 512 + 512  # incluye boot de respaldo


# ---------------------------------------------------------------- clonación
def _clone(src_img, dst_img, **opts):
    plat = ImagePlatform([src_img, dst_img])
    disks = plat.list_disks()
    plan = build_plan(disks[0], disks[1], CloneOptions(use_vss=False, **opts))
    assert plan.ok, plan.errors
    logs = []
    res = CloneEngine(plat, plan, log=lambda m, l="info": logs.append((l, m))).run()
    assert res.success, (res.message, logs)
    return plan, plat.list_disks()[1], logs


def _check_ntfs_files(img, part, files, tmp_path):
    out = str(tmp_path / f"chk_{part.start_lba}.bin")
    extract(img, part.start_lba * 512, part.sectors * 512, out)
    for name, data in files.items():
        got = subprocess.run(["ntfscat", out, name], capture_output=True, check=True).stdout
        assert got == data, name
    return out


@needs_tools
@pytest.mark.parametrize("verify", [False, True])
def test_clone_gpt_to_bigger(gpt_source, tmp_path, verify):
    img, parts, files = gpt_source
    dst = str(tmp_path / "dst.img")
    make_blank(dst, 1000)
    plan, tdisk, logs = _clone(img, dst, verify=verify)

    assert "No problems found" in run("sgdisk", "-v", dst).stdout
    assert tdisk.style == "GPT" and len(tdisk.partitions) == 4
    assert [p.fs for p in tdisk.partitions] == ["FAT32", "MSR", "NTFS", "NTFS"]
    src_disk = ImagePlatform([img]).list_disks()[0]
    assert tdisk.disk_guid != src_disk.disk_guid
    for s, t in zip(src_disk.partitions, tdisk.partitions):
        assert s.type_guid == t.type_guid and s.name == t.name
        assert s.unique_guid != t.unique_guid
        assert t.start_lba % 2048 == 0
    # C: creció hasta llenar el disco y la recuperación quedó al final
    c, rec = tdisk.partitions[2], tdisk.partitions[3]
    assert c.sectors > src_disk.partitions[2].sectors
    assert rec.sectors == src_disk.partitions[3].sectors
    assert rec.start_lba > src_disk.partitions[3].start_lba
    # Datos intactos y hidden sectors corregidos en la partición movida
    _check_ntfs_files(dst, c, files, tmp_path)
    with FileDevice(dst) as d:
        boot = d.read(rec.start_lba * 512, 512)
        assert struct.unpack_from("<I", boot, 0x1C)[0] == rec.start_lba
        nb = parse_ntfs_boot(boot)
        backup = d.read((rec.start_lba + nb.total_sectors) * 512, 512)
        assert struct.unpack_from("<I", backup, 0x1C)[0] == rec.start_lba
    # El NTFS de C: es consistente y ntfsresize puede ampliarlo al nuevo tamaño
    part = str(tmp_path / "c.bin")
    extract(dst, c.start_lba * 512, c.sectors * 512, part)
    r = subprocess.run(["ntfsresize", "-f", "-n", part], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    # EFI FAT32 sano
    efi = str(tmp_path / "efi.bin")
    p0 = tdisk.partitions[0]
    extract(dst, p0.start_lba * 512, p0.sectors * 512, efi)
    assert subprocess.run(["fsck.vfat", "-n", efi], capture_output=True).returncode == 0


@needs_tools
def test_clone_gpt_to_smaller_is_rejected(gpt_source, tmp_path):
    img, _, _ = gpt_source
    dst = str(tmp_path / "small.img")
    make_blank(dst, 450)
    plat = ImagePlatform([img, dst])
    s, t = plat.list_disks()
    plan = build_plan(s, t, CloneOptions())
    assert not plan.ok and plan.deficit_sectors > 0
    assert "Reduce primero" in " ".join(plan.errors)


@needs_tools
def test_clone_gpt_custom_size_leaves_unallocated(gpt_source, tmp_path):
    img, _, files = gpt_source
    dst = str(tmp_path / "dst.img")
    make_blank(dst, 1000)
    plat = ImagePlatform([img, dst])
    s, t = plat.list_disks()
    want = s.partitions[2].sectors + (100 * MiB) // 512
    plan = build_plan(s, t, CloneOptions(), resize_sectors=want)
    assert plan.ok
    res = CloneEngine(plat, plan, log=lambda *a: None).run()
    assert res.success
    td = plat.list_disks()[1]
    assert td.partitions[2].sectors == want
    assert "No problems found" in run("sgdisk", "-v", dst).stdout


@needs_tools
def test_clone_mbr_realigns_and_new_signature(mbr_source, tmp_path):
    img, files = mbr_source
    dst = str(tmp_path / "dst.img")
    make_blank(dst, 400)
    plan, tdisk, _ = _clone(img, dst)
    assert tdisk.style == "MBR"
    assert tdisk.signature not in (0, 0x1234ABCD)
    assert [p.start_lba % 2048 for p in tdisk.partitions] == [0, 0]
    assert tdisk.partitions[0].active and not tdisk.partitions[1].active
    with FileDevice(dst) as d:
        for p in tdisk.partitions:
            assert struct.unpack_from("<I", d.read(p.start_lba * 512, 512), 0x1C)[0] == p.start_lba
    _check_ntfs_files(dst, tdisk.partitions[1], files, tmp_path)
    out = run("sfdisk", "-d", dst).stdout
    assert "bootable" in out


@needs_tools
def test_clone_mbr_identical_keeps_offsets_and_signature(mbr_source, tmp_path):
    img, files = mbr_source
    dst = str(tmp_path / "dst.img")
    make_blank(dst, 400)
    plan, tdisk, logs = _clone(img, dst, id_mode="same")
    assert tdisk.signature == 0x1234ABCD
    assert [p.start_lba for p in tdisk.partitions] == [63, 102463]
    _check_ntfs_files(dst, tdisk.partitions[1], files, tmp_path)


def test_mounted_devices_mapping_gpt_and_mbr(tmp_path):
    from clondisk.core.model import Disk, Partition
    s = Disk(0, "a", "a", 100 * MiB, style="MBR", signature=0x11223344,
             partitions=[Partition(1, 63, 50000, mbr_type=7, active=True, fs="NTFS", has_windows=True)])
    t = Disk(1, "b", "b", 200 * MiB)
    plan = build_plan(s, t, CloneOptions())
    eng = CloneEngine(None, plan, log=lambda *a: None)
    (old, new), = eng.mounted_devices_mapping(identical=False)
    assert old == struct.pack("<IQ", 0x11223344, 63 * 512)
    assert new == struct.pack("<IQ", plan.new_signature, 2048 * 512)


@needs_tools
def test_cancel_leaves_no_partition_table(gpt_source, tmp_path):
    img, _, _ = gpt_source
    dst = str(tmp_path / "dst.img")
    make_blank(dst, 1000)
    plat = ImagePlatform([img, dst])
    s, t = plat.list_disks()
    plan = build_plan(s, t, CloneOptions(smart_copy=False))
    cancel = threading.Event()

    def progress(done, total, speed):
        if done > 20 * MiB:
            cancel.set()

    res = CloneEngine(plat, plan, log=lambda *a: None, progress=progress, cancel=cancel).run()
    assert res.cancelled and not res.success
    assert plat.list_disks()[1].style == "RAW"


@needs_tools
def test_dry_run_writes_nothing(gpt_source, tmp_path):
    img, _, _ = gpt_source
    dst = str(tmp_path / "dst.img")
    make_blank(dst, 1000)
    plat = ImagePlatform([img, dst])
    s, t = plat.list_disks()
    plan = build_plan(s, t, CloneOptions(dry_run=True))
    res = CloneEngine(plat, plan, log=lambda *a: None).run()
    assert res.success
    with open(dst, "rb") as f:
        assert f.read(4 * MiB) == b"\0" * (4 * MiB)


def test_planner_rejects_bitlocker_and_system_target():
    from clondisk.core.model import Disk, Partition
    s = Disk(0, "a", "a", 100 * MiB, style="GPT",
             partitions=[Partition(1, 2048, 50000, type_guid=GUID_BASIC, fs="BitLocker")])
    t = Disk(1, "b", "b", 200 * MiB, is_boot=True)
    plan = build_plan(s, t, CloneOptions())
    joined = " ".join(plan.errors)
    assert "BitLocker" in joined and "Windows en ejecución" in joined


def test_planner_sector_size_mismatch():
    from clondisk.core.model import Disk, Partition
    s = Disk(0, "a", "a", 100 * MiB, style="GPT",
             partitions=[Partition(1, 2048, 50000, type_guid=GUID_BASIC, fs="NTFS")])
    t = Disk(1, "b", "b", 200 * MiB, sector_size=4096)
    assert any("sector" in e for e in build_plan(s, t, CloneOptions()).errors)


# ================================================================ 1.1.0
import os
import shutil
from dataclasses import replace

from clondisk.core.copier import SECTOR_RETRIES, BadSectorError, Copier
from clondisk.core.device import Device, DeviceError
from clondisk.core.model import GUID_MSR, GUID_RECOVERY, Disk, Partition, WindowsInfo
from clondisk.core.planner import shrink_target_bytes
from clondisk.core.winprep import (DiskHealth, Hive, assess_health, bcdedit_partition_letters,
                                   choose_bcdboot, enable_boot_storage_drivers,
                                   gpt_conversion_status, match_oem_driver_folders)
from clondisk.platforms.base import Platform

GiB = 1024 * MiB
WIN10 = WindowsInfo("Windows 10 Pro", 19045, "Professional")


class RamDisk(Device):
    """Disco en memoria con sectores que fallan (siempre, o las primeras N lecturas)."""

    def __init__(self, size, ss=512, bad=(), flaky=None):
        self.buf = bytearray(size)
        self.size = size
        self.sector_size = ss
        self.path = "ram"
        self.bad = set(bad)
        self.flaky = dict(flaky or {})        # lba -> lecturas de un sector que fallan antes de ir bien
        self.reads = {}                       # lba -> lecturas de ese sector suelto

    def read(self, off, n):
        first, last = off // self.sector_size, (off + n - 1) // self.sector_size
        if n == self.sector_size:
            self.reads[first] = self.reads.get(first, 0) + 1
        for lba in range(first, last + 1):
            if lba in self.bad:
                raise DeviceError(f"CRC en LBA {lba}")
            if self.flaky.get(lba, 0) > 0:
                if n == self.sector_size:
                    self.flaky[lba] -= 1
                raise DeviceError(f"error transitorio en LBA {lba}")
        return bytes(self.buf[off:off + n])

    def write(self, off, data):
        self.buf[off:off + len(data)] = data


class RecDev(Device):
    """Solo registra escrituras (para discos de cientos de GB)."""

    def __init__(self, size, ss=512):
        self.size, self.sector_size, self.path = size, ss, "rec"
        self.writes, self.head = [], b""

    def write(self, off, data):
        self.writes.append((off, len(data)))
        if off == 0:
            self.head = bytes(data[:self.sector_size])

    def covered(self, lo, hi):
        return all(any(o <= x < o + n for o, n in self.writes) for x in range(lo, hi, self.sector_size))


def real_source():
    """Disco 1 del caso real: ST500DM002, MBR, Reservado (activa) + E: (Windows) + Recuperación."""
    return Disk(1, r"\\.\PhysicalDrive1", "ST500DM002-1BD142", 976773168 * 512, style="MBR",
                signature=0x5A3E1F02, boot_code=b"\x33\xC0\x8E\xD0" + b"\x90" * 436, partitions=[
                    Partition(1, 2048, 204800, mbr_type=0x07, active=True, is_system=True, fs="NTFS",
                              letter="D", label="Reservado para el sistema", used_bytes=32 * MiB),
                    Partition(2, 206848, 975642624, mbr_type=0x07, fs="NTFS", letter="E",
                              used_bytes=int(51.7 * GiB), has_windows=True, win_info=WIN10),
                    Partition(3, 975849472, 921600, mbr_type=0x27, fs="NTFS", label="Recuperación")])


def real_target():
    """Disco 2: SSD de 120 GB (111.8 GB) con una GPT previa."""
    return Disk(2, r"\\.\PhysicalDrive2", "SSD 120GB", 234441648 * 512, style="GPT", partitions=[
        Partition(1, 2048, 204800, type_guid=GUID_EFI, fs="FAT32"),
        Partition(2, 206848, 32768, type_guid=GUID_MSR, fs="MSR"),
        Partition(3, 239616, 233000000, type_guid=GUID_BASIC, fs="NTFS", letter="F",
                  used_bytes=int(37.6 * GiB)),
        Partition(4, 233239616, 1200000, type_guid=GUID_RECOVERY, fs="NTFS")])


def _shrunk(src, plan):
    new_size = shrink_target_bytes(plan)
    assert new_size and new_size % MiB == 0
    parts = [replace(p, sectors=new_size // 512) if p.index == plan.resizable_index else p
             for p in src.partitions]
    return replace(src, partitions=parts), new_size


@pytest.mark.parametrize("convert", [False, True])
def test_real_case_plan_needs_shrink_then_fits(convert):
    src, tgt = real_source(), real_target()
    opts = CloneOptions(convert_to_gpt=convert)
    plan = build_plan(src, tgt, opts)
    assert not plan.ok and plan.deficit_sectors > 0
    assert any("Reduce primero la partición E:" in e for e in plan.errors)

    src2, new_size = _shrunk(src, plan)
    assert 51.7 * GiB < new_size < tgt.size_bytes
    plan = build_plan(src2, tgt, opts)
    assert plan.ok, plan.errors
    total = tgt.size_bytes // 512
    assert plan.source.style == "MBR" and plan.firmware == "BIOS"
    assert plan.final_firmware == ("UEFI" if convert else "BIOS")
    assert all(pp.new_start_lba % 2048 == 0 for pp in plan.parts)
    end = max(pp.new_start_lba + pp.new_sectors for pp in plan.parts)
    assert end <= total
    if convert:
        assert (total - end) * 512 >= MiB          # sitio para la GPT de respaldo de mbr2gpt
    assert [pp.src.active for pp in plan.parts] == [True, False, False]
    assert plan.system_part().src.label == "Reservado para el sistema"
    assert plan.windows_part().src.letter == "E"

    eng = CloneEngine(None, plan, log=lambda *a: None)
    dev = RecDev(tgt.size_bytes)
    eng._wipe_tables(dev)
    assert dev.covered(0, 34 * 512)                              # GPT primaria vieja
    assert dev.covered((total - 33) * 512, total * 512)          # GPT de respaldo vieja
    dev.writes.clear()
    eng._write_table(dev, plan.final_ids(identical=False))
    code, sig, entries = parse_mbr(dev.head)
    assert code == src.boot_code and sig == plan.new_signature != src.signature
    assert [e.active for e in entries] == [True, False, False]
    assert [e.ptype for e in entries] == [0x07, 0x07, 0x27]
    assert [(e.start_lba, e.sectors) for e in entries] == \
           [(pp.new_start_lba, pp.new_sectors) for pp in plan.parts]


def test_convert_requires_new_ids_and_bcdboot():
    src, tgt = real_source(), real_target()
    plan = build_plan(src, tgt, CloneOptions(convert_to_gpt=True, id_mode="same"))
    assert any("Nuevos identificadores" in e for e in plan.errors)
    plan = build_plan(src, tgt, CloneOptions(convert_to_gpt=True, fix_boot=False))
    assert any("bcdboot" in e for e in plan.errors)


# ---------------------------------------------------------------- Copier
def _copier(**kw):
    return Copier(threading.Event(), chunk_size=64 * 1024, **kw)


def test_copier_retries_transient_errors():
    src = RamDisk(4 * MiB, flaky={300: SECTOR_RETRIES - 1})
    src.buf[:] = os.urandom(len(src.buf))
    dst = RamDisk(4 * MiB)
    c = _copier()
    c.copy(src, 0, dst, 0, [(0, 4 * MiB)])
    assert dst.buf == src.buf and c.bad_sectors == []


def test_copier_skips_bad_sectors_with_zeros():
    base_lba = 1000
    bad = {base_lba + 7, base_lba + 8, base_lba + 5000}
    src = RamDisk(8 * MiB, bad=bad)
    src.buf[:] = os.urandom(len(src.buf))
    dst = RamDisk(8 * MiB)
    seen = []
    c = _copier(skip_bad_sectors=True, on_bad_sector=seen.append)
    rel = [(0, 4 * MiB)]
    c.copy(src, base_lba * 512, dst, 2048 * 512, rel, lba_base=base_lba)
    assert sorted(c.bad_sectors) == sorted(bad) == sorted(seen)
    for lba in range(base_lba, base_lba + 8192):
        got = dst.buf[(lba - base_lba + 2048) * 512:(lba - base_lba + 2049) * 512]
        want = b"\0" * 512 if lba in bad else src.buf[lba * 512:(lba + 1) * 512]
        assert got == want, lba
    assert all(src.reads[lba] == SECTOR_RETRIES for lba in bad)   # máximo 3 intentos por sector
    c.verify(src, base_lba * 512, dst, 2048 * 512, rel, lba_base=base_lba)


def test_copier_aborts_with_exact_lba():
    src = RamDisk(4 * MiB, bad={4321})
    dst = RamDisk(4 * MiB)
    with pytest.raises(BadSectorError) as ei:
        _copier().copy(src, 0, dst, 0, [(0, 4 * MiB)])
    assert ei.value.lba == 4321 and "LBA 4321" in str(ei.value)


# ---------------------------------------------------------------- motor sin herramientas de disco
def _fake_ntfs_boot(hidden, total_sectors):
    b = bytearray(512)
    b[3:11] = b"NTFS    "
    struct.pack_into("<HB", b, 0x0B, 512, 8)
    struct.pack_into("<I", b, 0x1C, hidden)
    struct.pack_into("<QQ", b, 0x28, total_sectors, 4)
    b[0x40] = 0xF6
    b[510:512] = b"\x55\xAA"
    return bytes(b)


class RamPlatform(Platform):
    def __init__(self, devs):
        self.devs = devs

    def open_disk(self, disk, write=False):
        return self.devs[disk.number]

    def disk_health(self, disk):
        return DiskHealth("Healthy", 0, 35, None, True)


def _ram_clone(skip, bad):
    src = RamDisk(64 * MiB)
    entries = [MbrEntry(0, 0x80, 0x07, 63, 20000), MbrEntry(1, 0, 0x07, 20063, 60000)]
    src.write(0, build_mbr(b"\x33\xC0" + b"\x90" * 438, 0x1234ABCD, entries))
    for e in entries:
        src.write(e.start_lba * 512, _fake_ntfs_boot(e.start_lba, e.sectors - 1))
    sdisk = scan_disk(src, 0, "hdd", compute_usage=False)
    src.bad = set(bad)
    sdisk.partitions[1].has_windows = True
    dst = RamDisk(64 * MiB)
    tdisk = Disk(1, "ssd", "ssd", dst.size)
    plan = build_plan(sdisk, tdisk, CloneOptions(use_vss=False, smart_copy=False, retrim=False,
                                                 skip_bad_sectors=skip))
    assert plan.ok, plan.errors
    res = CloneEngine(RamPlatform({0: src, 1: dst}), plan, log=lambda *a: None).run()
    return plan, res, dst


def test_engine_skip_bad_sectors_and_boot_checks():
    plan, res, dst = _ram_clone(True, {30000, 30001})
    assert res.success and res.bad_sectors == [30000, 30001]
    new = plan.parts[1].new_start_lba + (30000 - 20063)
    assert dst.buf[new * 512:(new + 2) * 512] == b"\0" * 1024
    texts = [t for _, t in res.boot_checks]
    assert res.boot_ok, res.boot_checks
    assert any("55AA" in t for t in texts) and any("activa" in t for t in texts)
    assert sum("Hidden sectors" in t for t in texts) == 2


def test_engine_aborts_on_bad_sector_and_leaves_target_empty():
    _, res, dst = _ram_clone(False, {30000})
    assert not res.success and "LBA 30000" in res.message
    assert dst.buf[:512] == b"\0" * 512


def test_boot_check_flags_wrong_hidden_sectors():
    plan, res, dst = _ram_clone(True, set())
    p = plan.parts[1]
    struct.pack_into("<I", dst.buf, p.new_start_lba * 512 + 0x1C, 63)
    eng = CloneEngine(RamPlatform({1: dst}), plan, log=lambda *a: None)
    eng._verify_boot()
    bad = [t for ok, t in eng.boot_checks if not ok]
    assert len(bad) == 1 and "esperado" in bad[0]


# ---------------------------------------------------------------- conversión GPT / bcdboot
def _src_with(win_info=WIN10, nparts=3, style="MBR"):
    d = real_source()
    d.style = style
    d.partitions[1].win_info = win_info
    if nparts == 4:
        d.partitions.append(Partition(4, 976771072, 2048, mbr_type=0x07, fs="NTFS"))
    return d


@pytest.mark.parametrize("kw, host, tool, allowed, needle", [
    ({}, 19045, True, True, ""),
    ({}, 15063, True, True, ""),
    ({}, 14393, True, False, "1703"),
    ({}, None, True, False, "15063"),
    ({}, 19045, False, False, "mbr2gpt.exe"),
    ({"nparts": 4}, 19045, True, False, "máximo 3"),
    ({"win_info": WindowsInfo("Windows 7 Professional", 7601, "Professional")}, 19045, True, False,
     "Legacy/CSM"),
    ({"win_info": None}, 19045, True, False, "Legacy/CSM"),
    ({"style": "GPT"}, 19045, True, False, "ya es GPT"),
])
def test_gpt_conversion_status(kw, host, tool, allowed, needle):
    ok, reason = gpt_conversion_status(_src_with(**kw), host, tool)
    assert ok is allowed and needle in reason


def test_windows_info_display_win11():
    assert WindowsInfo("Windows 10 Pro", 22631, "Professional").display().startswith("Windows 11 Pro")
    assert not WindowsInfo("Windows 7", 7601).is_win10_or_later


@pytest.mark.parametrize("host, win, exists, use_target", [
    (19045, 22631, True, True),     # Windows clonado más nuevo → su propio bcdboot
    (19045, 22631, False, False),   # ... pero sin bcdboot.exe → el del anfitrión
    (19045, 19045, True, False),
    (19045, 7601, True, False),
    (19045, None, True, False),
])
def test_choose_bcdboot(host, win, exists, use_target):
    exe, why = choose_bcdboot(host, win, "HOST", "TARGET", exists)
    assert exe == ("TARGET" if use_target else "HOST") and why


def test_bcdedit_letters_any_language():
    out = ("Cargador de arranque de Windows\n-------------------------------\n"
           "identificador           {current}\ndevice                  partition=G:\n"
           "path                    \\Windows\\system32\\winload.exe\n"
           "osdevice                partition=G:\n")
    assert bcdedit_partition_letters(out) == {"G"}
    assert bcdedit_partition_letters("device unknown") == set()


# ---------------------------------------------------------------- salud y drivers
def test_assess_health():
    assert assess_health(None)[0] == "unknown"
    assert assess_health(DiskHealth("Healthy", 0, 40, None, True))[0] == "ok"
    lvl, msgs = assess_health(DiskHealth("Healthy", 12, 40, None, True))
    assert lvl == "danger" and any("12" in m for m in msgs)
    lvl, msgs = assess_health(DiskHealth("Warning"))
    assert lvl == "danger" and any("no están disponibles" in m for m in msgs)
    lvl, msgs = assess_health(DiskHealth("Healthy"))
    assert lvl == "ok" and any("no están disponibles" in m for m in msgs)


def test_match_oem_driver_folders():
    oem = {"oem3.inf": "aa", "oem10.inf": "bb", "oem11.inf": "zz"}
    repo = {"plotter.inf_amd64_1": ["aa"], "usb.inf_amd64_2": ["cc", "bb"], "x.inf_amd64_3": []}
    found, missing = match_oem_driver_folders(oem, repo)
    assert found == {"oem3.inf": "plotter.inf_amd64_1", "oem10.inf": "usb.inf_amd64_2"}
    assert missing == ["oem11.inf"]


# ---------------------------------------------------------------- colmena SYSTEM simulada
class DictHive(Hive):
    def __init__(self, keys):
        self.keys = {k.lower(): dict(v) for k, v in keys.items()}

    def key_exists(self, path):
        return path.lower() in self.keys

    def get_value(self, path, name):
        return self.keys.get(path.lower(), {}).get(name)

    def set_dword(self, path, name, value):
        self.keys[path.lower()][name] = value

    def delete_key(self, path):
        p = path.lower()
        for k in [k for k in self.keys if k == p or k.startswith(p + "\\")]:
            del self.keys[k]


def test_enable_boot_storage_drivers():
    svc1, svc2 = "ControlSet001\\Services\\", "ControlSet002\\Services\\"
    hive = DictHive({
        "Select": {"Current": 2},
        svc1 + "storahci": {"Start": 3},
        svc2 + "storahci": {"Start": 3},
        svc2 + "storahci\\StartOverride": {"0": 3},
        svc2 + "stornvme": {"Start": 0},
        svc2 + "iaStorAVC": {"Start": 3},
        svc2 + "iaStorAVC\\StartOverride": {"0": 3},
        svc2 + "iaStorAVC\\StartOverride\\Sub": {},
    })
    logs = []
    changes = enable_boot_storage_drivers(hive, logs.append)
    assert hive.get_value(svc2 + "storahci", "Start") == 0
    assert hive.get_value(svc2 + "iaStorAVC", "Start") == 0
    assert hive.get_value(svc2 + "stornvme", "Start") == 0
    assert not hive.key_exists(svc2 + "storahci\\StartOverride")
    assert not any("iastoravc\\startoverride" in k for k in hive.keys)
    assert not hive.key_exists(svc2 + "iaStorV")                 # no se crea lo que no existe
    assert hive.get_value(svc1 + "storahci", "Start") == 3       # otro ControlSet intacto
    assert logs == changes and len(changes) == 4


def test_enable_boot_storage_drivers_requires_select():
    with pytest.raises(ValueError):
        enable_boot_storage_drivers(DictHive({}))


# ---------------------------------------------------------------- laboratorio: reducir con ntfsresize
@needs_tools
@pytest.mark.skipif(not shutil.which("ntfsresize"), reason="falta ntfsresize")
def test_lab_shrink_partition_with_ntfsresize(mbr_source, tmp_path):
    img, files = mbr_source
    plat = ImagePlatform([img])
    disk = plat.list_disks()[0]
    part = disk.partitions[1]
    new_size = part.size_bytes(512) - 40 * MiB
    plat.shrink_partition(disk, part, new_size, lambda *a: None)
    after = plat.list_disks()[0].partitions[1]
    assert after.start_lba == part.start_lba and after.sectors * 512 == new_size
    _check_ntfs_files(img, after, files, tmp_path)
    chk = str(tmp_path / "shrunk.bin")
    extract(img, after.start_lba * 512, new_size, chk)
    r = subprocess.run(["ntfsresize", "-f", "-n", "-i", chk], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
