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
