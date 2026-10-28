"""Fixtures: crea imágenes de disco reales con sgdisk/sfdisk, mkntfs y mkfs.vfat."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

MiB = 1024 * 1024
TOOLS = ["sgdisk", "sfdisk", "mkntfs", "mkfs.vfat", "ntfscp", "ntfscat", "ntfsinfo", "fsck.vfat"]
have_tools = all(shutil.which(t) for t in TOOLS)
needs_tools = pytest.mark.skipif(not have_tools, reason="faltan herramientas de disco")


def run(*cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def make_blank(path, size_mib):
    with open(path, "wb") as f:
        f.truncate(size_mib * MiB)


def extract(img, offset, size, out):
    with open(img, "rb") as f, open(out, "wb") as o:
        f.seek(offset)
        remaining = size
        while remaining:
            b = f.read(min(remaining, 8 * MiB))
            o.write(b)
            remaining -= len(b)


def inject(img, offset, part_file):
    with open(img, "r+b") as f, open(part_file, "rb") as p:
        f.seek(offset)
        shutil.copyfileobj(p, f)


def build_fs_into(img, workdir, offset, size, fs, label, files=None, hidden_lba=None):
    part = os.path.join(workdir, f"p_{offset}.bin")
    with open(part, "wb") as f:
        f.truncate(size)
    if fs == "ntfs":
        args = ["mkntfs", "-F", "-Q", "-L", label]
        if hidden_lba is not None:
            args += ["-p", str(hidden_lba)]
        run(*args, part)
        for name, data in (files or {}).items():
            src = os.path.join(workdir, "tmp_" + name)
            with open(src, "wb") as fh:
                fh.write(data)
            run("ntfscp", "-f", part, src, name)
    else:
        args = ["mkfs.vfat", "-F", "32", "-n", label]
        if hidden_lba is not None:
            args += ["-h", str(hidden_lba)]
        run(*args, part)
    inject(img, offset, part)
    os.remove(part)


@pytest.fixture
def gpt_source(tmp_path):
    """Disco GPT estilo Windows: EFI + MSR + C: (NTFS con archivos) + Recuperación."""
    img = str(tmp_path / "src_gpt.img")
    make_blank(img, 600)
    run("sgdisk", "-n1:1M:+100M", "-t1:EF00", "-c1:EFI system partition",
        "-n2:0:+16M", "-t2:0C01", "-c2:Microsoft reserved partition",
        "-n3:0:+380M", "-t3:0700", "-c3:Basic data partition",
        "-n4:0:0", "-t4:2700", "-c4:Basic data partition", img)
    info = run("sgdisk", "-p", img).stdout
    parts = {}
    for line in info.splitlines():
        cols = line.split()
        if cols and cols[0].isdigit():
            parts[int(cols[0])] = (int(cols[1]), int(cols[2]))
    files = {
        "a.bin": os.urandom(3 * MiB),
        "b.txt": b"Hola desde ClonDisk\n" * 5000,
        "c.bin": os.urandom(12 * MiB + 1234),
    }
    s, e = parts[1]
    build_fs_into(img, str(tmp_path), s * 512, (e - s + 1) * 512, "vfat", "SYSTEM", hidden_lba=s)
    s, e = parts[3]
    build_fs_into(img, str(tmp_path), s * 512, (e - s + 1) * 512, "ntfs", "Windows", files, hidden_lba=s)
    s, e = parts[4]
    build_fs_into(img, str(tmp_path), s * 512, (e - s + 1) * 512, "ntfs", "Recovery", hidden_lba=s)
    return img, parts, files


@pytest.fixture
def mbr_source(tmp_path):
    """Disco MBR clásico desalineado (estilo XP): sistema reservado activo + C:."""
    img = str(tmp_path / "src_mbr.img")
    make_blank(img, 300)
    layout = "label: dos\nlabel-id: 0x1234abcd\nstart=63, size=102400, type=7, bootable\nstart=102463, size=409600, type=7\n"
    subprocess.run(["sfdisk", "--no-reread", "-q", img], input=layout, text=True, check=True,
                   capture_output=True)
    files = {"datos.bin": os.urandom(5 * MiB)}
    build_fs_into(img, str(tmp_path), 63 * 512, 102400 * 512, "ntfs", "Reservado", hidden_lba=63)
    build_fs_into(img, str(tmp_path), 102463 * 512, 409600 * 512, "ntfs", "Sistema", files,
                  hidden_lba=102463)
    return img, files
