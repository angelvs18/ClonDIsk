"""Backend para Windows 10/11: acceso raw con Win32, VSS, PowerShell y diskpart.

Requiere ejecutarse como administrador y Python de 64 bits.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import gc
import hashlib
import json
import re
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

from ..core.device import Device, DeviceError
from ..core.fs import ntfs_used_bytes
from ..core.model import Disk, Partition, WindowsInfo, fmt_size
from ..core.scan import scan_disk
from ..core.winprep import (DiskHealth, DriverBackup, Hive, bcdedit_partition_letters,
                            choose_bcdboot, count_inf_files, driver_backup_dir,
                            enable_boot_storage_drivers, match_oem_driver_folders,
                            write_denied_advice)
from .base import LogFn, Platform

# --- Constantes Win32 -------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_BEGIN = 0
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
IOCTL_DISK_DELETE_DRIVE_LAYOUT = 0x0007C100
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
FSCTL_ALLOW_EXTENDED_DASD_IO = 0x00090083
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020

CREATE_NO_WINDOW = 0x08000000

VSS_ERRORS = {
    1: "acceso denegado", 2: "argumento inválido", 3: "volumen no encontrado",
    4: "volumen no compatible con instantáneas", 5: "contexto no compatible",
    6: "espacio insuficiente para la instantánea", 7: "volumen en uso",
    8: "se alcanzó el máximo de instantáneas", 9: "otra operación de instantánea en curso",
    10: "el proveedor rechazó la operación", 11: "proveedor no registrado",
    12: "fallo del proveedor VSS", 13: "error desconocido",
}


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(argv: List[str]) -> bool:
    """Relanza el proceso con UAC. Devuelve True si se lanzó."""
    params = subprocess.list2cmdline(argv)
    exe = sys.executable
    workdir = None
    if not getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(["-m", "clondisk", *argv])
        # Los procesos elevados arrancan en System32: indicar la carpeta del proyecto.
        workdir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
    return rc > 32


def _system32() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    sysnative = os.path.join(root, "Sysnative")
    if struct.calcsize("P") == 4 and os.path.isdir(sysnative):
        return sysnative
    return os.path.join(root, "System32")


# ---------------------------------------------------------------------------
# Acceso a dispositivos con Win32
# ---------------------------------------------------------------------------
class _K32:
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            from ctypes import wintypes as wt
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
            k.CreateFileW.restype = wt.HANDLE
            k.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
            k.ReadFile.restype = wt.BOOL
            k.WriteFile.argtypes = [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
            k.WriteFile.restype = wt.BOOL
            k.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
            k.SetFilePointerEx.restype = wt.BOOL
            k.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD, wt.LPVOID, wt.DWORD,
                                          ctypes.POINTER(wt.DWORD), wt.LPVOID]
            k.DeviceIoControl.restype = wt.BOOL
            k.FlushFileBuffers.argtypes = [wt.HANDLE]
            k.FlushFileBuffers.restype = wt.BOOL
            k.CloseHandle.argtypes = [wt.HANDLE]
            k.CloseHandle.restype = wt.BOOL
            k.GetLogicalDrives.restype = wt.DWORD
            cls._inst = k
        return cls._inst


def _winerr(msg: str) -> DeviceError:
    code = ctypes.get_last_error()
    return DeviceError(f"{msg}: {ctypes.FormatError(code).strip()} (código {code})", code)


class WinDevice(Device):
    def __init__(self, path: str, write: bool = False, sector_size: Optional[int] = None,
                 extended_dasd: bool = False):
        from ctypes import wintypes as wt
        self._wt = wt
        self.k = _K32.get()
        self.path = path
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        flags = FILE_FLAG_WRITE_THROUGH if write else 0
        h = self.k.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                               OPEN_EXISTING, flags, None)
        if h is None or h == INVALID_HANDLE_VALUE:
            raise _winerr(f"No se pudo abrir {path}")
        self.h = h
        self._lock = threading.Lock()
        self.extended_dasd = False
        if extended_dasd:
            try:
                self.ioctl(FSCTL_ALLOW_EXTENDED_DASD_IO, out_size=0)
                self.extended_dasd = True
            except DeviceError:
                self.extended_dasd = False
        self.size = struct.unpack("<q", self.ioctl(IOCTL_DISK_GET_LENGTH_INFO, out_size=8))[0]
        if sector_size is None:
            try:
                geo = self.ioctl(IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, out_size=256)
                sector_size = struct.unpack_from("<I", geo, 20)[0]
            except DeviceError:
                sector_size = 512
        self.sector_size = sector_size or 512

    def ioctl(self, code: int, in_buf: bytes = b"", out_size: int = 0, check: bool = True) -> bytes:
        wt = self._wt
        out = ctypes.create_string_buffer(max(out_size, 1))
        ret = wt.DWORD(0)
        inb = ctypes.create_string_buffer(in_buf, len(in_buf)) if in_buf else None
        ok = self.k.DeviceIoControl(self.h, code, inb, len(in_buf), out if out_size else None,
                                    out_size, ctypes.byref(ret), None)
        if not ok and check:
            raise _winerr(f"DeviceIoControl 0x{code:08X} sobre {self.path}")
        return out.raw[:ret.value]

    def _seek(self, offset: int):
        if not self.k.SetFilePointerEx(self.h, offset, None, FILE_BEGIN):
            raise _winerr(f"No se pudo posicionar en {offset} de {self.path}")

    def read(self, offset: int, length: int) -> bytes:
        wt = self._wt
        buf = ctypes.create_string_buffer(length)
        with self._lock:
            self._seek(offset)
            got = wt.DWORD(0)
            if not self.k.ReadFile(self.h, buf, length, ctypes.byref(got), None):
                raise _winerr(f"Error de lectura en {self.path} @ {offset}")
        if got.value != length:
            raise DeviceError(f"Lectura incompleta en {self.path} @ {offset}: {got.value}/{length}")
        return buf.raw

    def write(self, offset: int, data: bytes) -> None:
        wt = self._wt
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        with self._lock:
            self._seek(offset)
            put = wt.DWORD(0)
            if not self.k.WriteFile(self.h, buf, len(data), ctypes.byref(put), None):
                raise _winerr(f"Error de escritura en {self.path} @ {offset}")
        if put.value != len(data):
            raise DeviceError(f"Escritura incompleta en {self.path} @ {offset}")

    def flush(self) -> None:
        with self._lock:
            self.k.FlushFileBuffers(self.h)

    def close(self) -> None:
        if getattr(self, "h", None):
            self.k.CloseHandle(self.h)
            self.h = None


# ---------------------------------------------------------------------------
# PowerShell / diskpart
# ---------------------------------------------------------------------------
class CommandError(RuntimeError):
    pass


def run_ps(script: str, timeout: int = 600) -> str:
    wrapped = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;\n"
        "try {\n" + script + "\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )
    enc = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", enc],
        capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() or out.strip()
        raise CommandError(err or f"PowerShell terminó con código {r.returncode}")
    return out


def run_ps_json(script: str, timeout: int = 600):
    out = run_ps(script, timeout).lstrip("\ufeff").strip()
    return json.loads(out) if out else None


def ps_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_diskpart(lines: List[str], timeout: int = 600) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="clondisk_dp_")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write("\n".join(lines) + "\n")
        r = subprocess.run([os.path.join(_system32(), "diskpart.exe"), "/s", path],
                           capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        out = r.stdout.decode("oem", "replace") if is_windows() else r.stdout.decode()
        if r.returncode != 0:
            raise CommandError(f"diskpart falló (código {r.returncode}):\n{out.strip()}")
        return out
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _decode_log(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw[1:2] == b"\0":
        return raw.decode("utf-16-le", "replace").lstrip("\ufeff")
    return raw.decode("utf-8" if raw.startswith(b"\xef\xbb\xbf") else "mbcs", "replace").lstrip("\ufeff")


FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"


def _documents_dir() -> Optional[str]:
    """Carpeta «Documentos» real del usuario con SHGetKnownFolderPath(FOLDERID_Documents):
    respeta la redirección (OneDrive, otra unidad). ``None`` si no se puede obtener; no se
    arma la ruta a partir de %USERPROFILE%, porque puede no existir."""
    import uuid
    from ctypes import wintypes as wt
    try:
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(FOLDERID_DOCUMENTS).bytes_le)
        path = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wt.DWORD, wt.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path))
        try:
            return path.value if hr == 0 and path.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path)
    except Exception:
        return None


def _file_digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class WinregHive(Hive):
    """Colmena cargada en ``HKLM\\<root>`` (con ``reg load``)."""

    def __init__(self, root: str):
        import winreg
        self.winreg = winreg
        self.root = root

    def _open(self, path: str, write: bool = False):
        w = self.winreg
        access = w.KEY_READ | (w.KEY_WRITE if write else 0) | w.KEY_WOW64_64KEY
        return w.OpenKey(w.HKEY_LOCAL_MACHINE, f"{self.root}\\{path}", 0, access)

    def key_exists(self, path: str) -> bool:
        try:
            self._open(path).Close()
            return True
        except OSError:
            return False

    def get_value(self, path: str, name: str):
        try:
            with self._open(path) as k:
                return self.winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def set_dword(self, path: str, name: str, value: int) -> None:
        with self._open(path, write=True) as k:
            self.winreg.SetValueEx(k, name, 0, self.winreg.REG_DWORD, int(value))

    def delete_key(self, path: str) -> None:
        w = self.winreg
        with self._open(path, write=True) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(w.EnumKey(k, i))
                except OSError:
                    break
                i += 1
        for sub in subs:
            self.delete_key(f"{path}\\{sub}")
        parent, _, leaf = path.rpartition("\\")
        with self._open(parent, write=True) as k:
            w.DeleteKey(k, leaf)


def run_exe(args: List[str], timeout: int = 600) -> str:
    r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    out = (r.stdout + r.stderr).decode("oem" if is_windows() else "utf-8", "replace")
    if r.returncode != 0:
        raise CommandError(f"{os.path.basename(args[0])} falló (código {r.returncode}): {out.strip()}")
    return out


LIST_DISKS_PS = r"""
$out = @()
$phys = @{}
try { foreach ($pd in Get-PhysicalDisk) { $phys["$($pd.DeviceId)"] = $pd } } catch {}
foreach ($d in Get-Disk) {
  $parts = @()
  foreach ($p in @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue)) {
    $v = $null
    try { $v = $p | Get-Volume -ErrorAction Stop } catch {}
    $letter = $null
    if ($p.DriveLetter -and [int][char]$p.DriveLetter -ne 0) { $letter = [string]$p.DriveLetter }
    $parts += [pscustomobject]@{
      Number = $p.PartitionNumber; Offset = [uint64]$p.Offset; Size = [uint64]$p.Size
      DriveLetter = $letter; IsBoot = [bool]$p.IsBoot; IsSystem = [bool]$p.IsSystem
      FileSystem = $(if ($v) { [string]$v.FileSystem } else { $null })
      Label = $(if ($v) { [string]$v.FileSystemLabel } else { $null })
      VSize = $(if ($v) { [uint64]$v.Size } else { $null })
      VFree = $(if ($v) { [uint64]$v.SizeRemaining } else { $null })
      VPath = $(if ($v) { [string]$v.Path } else { $null })
    }
  }
  $pd = $phys["$($d.Number)"]
  $out += [pscustomobject]@{
    Number = $d.Number; FriendlyName = [string]$d.FriendlyName; Serial = [string]$d.SerialNumber
    Size = [uint64]$d.Size; LogicalSectorSize = [int]$d.LogicalSectorSize
    PhysicalSectorSize = [int]$d.PhysicalSectorSize; PartitionStyle = [string]$d.PartitionStyle
    BusType = [string]$d.BusType; MediaType = $(if ($pd) { [string]$pd.MediaType } else { 'Unspecified' })
    IsBoot = [bool]$d.IsBoot; IsSystem = [bool]$d.IsSystem; IsOffline = [bool]$d.IsOffline
    IsReadOnly = [bool]$d.IsReadOnly; Partitions = $parts
  }
}
ConvertTo-Json -InputObject @($out) -Depth 5 -Compress
"""


class WindowsPlatform(Platform):
    name = "Windows"
    supports_vss = True
    supports_offline_windows = True

    def __init__(self):
        self._locked_volumes: List[WinDevice] = []
        self._win_info_cache: Dict[tuple, Optional[WindowsInfo]] = {}

    # ------------------------------------------------------------------
    def list_disks(self) -> List[Disk]:
        data = run_ps_json(LIST_DISKS_PS) or []
        disks: List[Disk] = []
        for d in data:
            n = int(d["Number"])
            path = rf"\\.\PhysicalDrive{n}"
            ss = int(d.get("LogicalSectorSize") or 512)
            model = (d.get("FriendlyName") or f"Disco {n}").strip()
            try:
                with WinDevice(path, sector_size=ss) as dev:
                    disk = scan_disk(dev, n, model, compute_usage=False, size_bytes=int(d["Size"]))
                    self._annotate(disk, d)
                    for p in disk.partitions:
                        if p.fs == "NTFS" and p.used_bytes is None:
                            try:
                                p.used_bytes = ntfs_used_bytes(dev, p.offset_bytes(ss), p.size_bytes(ss))
                            except Exception:
                                pass
            except Exception as e:
                disk = Disk(number=n, path=path, model=model, size_bytes=int(d.get("Size") or 0),
                            sector_size=ss)
                self._annotate(disk, d)
                disk.problems.append(f"No se pudo leer el disco: {e}")
            disks.append(disk)
        return disks

    def _annotate(self, disk: Disk, d: dict):
        disk.sector_size = int(d.get("LogicalSectorSize") or disk.sector_size)
        disk.physical_sector_size = int(d.get("PhysicalSectorSize") or disk.sector_size)
        media = (d.get("MediaType") or "").upper()
        disk.media = "SSD" if media == "SSD" else "HDD" if media == "HDD" else "Desconocido"
        disk.bus = d.get("BusType") or ""
        disk.serial = (d.get("Serial") or "").strip()
        disk.is_boot = bool(d.get("IsBoot"))
        disk.is_system = bool(d.get("IsSystem"))
        disk.is_offline = bool(d.get("IsOffline"))
        disk.is_readonly = bool(d.get("IsReadOnly"))
        by_offset = {int(p["Offset"]): p for p in (d.get("Partitions") or [])}
        for part in disk.partitions:
            info = by_offset.get(part.offset_bytes(disk.sector_size))
            if not info:
                continue
            part.letter = info.get("DriveLetter") or None
            if info.get("Label"):
                part.label = info["Label"]
            part.volume_path = info.get("VPath") or None
            part.is_boot = bool(info.get("IsBoot"))
            part.is_system = part.is_system or bool(info.get("IsSystem"))
            if info.get("VSize") and info.get("VFree") is not None:
                part.used_bytes = int(info["VSize"]) - int(info["VFree"])
            if part.letter:
                part.has_windows = os.path.exists(
                    f"{part.letter}:\\Windows\\System32\\config\\SYSTEM")
            elif part.is_boot:
                part.has_windows = True
            if part.has_windows:
                try:
                    part.win_info = self.windows_info(part)
                except Exception:
                    part.win_info = None

    # ------------------------------------------------------------------ versión de Windows
    @staticmethod
    def _read_version_key(root_key, subpath: str) -> WindowsInfo:
        import winreg
        with winreg.OpenKey(root_key, subpath, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            def val(name):
                try:
                    return winreg.QueryValueEx(k, name)[0]
                except OSError:
                    return None
            build = val("CurrentBuild") or val("CurrentBuildNumber")
            try:
                build = int(str(build).strip()) if build is not None else None
            except ValueError:
                build = None
            return WindowsInfo(product_name=str(val("ProductName") or ""), build=build,
                               edition=str(val("EditionID") or ""))

    def windows_info(self, part: Partition) -> Optional[WindowsInfo]:
        """Lee ProductName, CurrentBuild y EditionID. Para un Windows sin conexión se carga
        una COPIA de su colmena SOFTWARE: el volumen de origen no se modifica."""
        import winreg
        if part.is_boot:
            return self._read_version_key(winreg.HKEY_LOCAL_MACHINE,
                                          r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        if not part.letter:
            return None
        cfg = f"{part.letter}:\\Windows\\System32\\config"
        hive = os.path.join(cfg, "SOFTWARE")
        st = os.stat(hive)
        key = (part.volume_path or part.letter, st.st_size, st.st_mtime_ns)
        if key in self._win_info_cache:
            return self._win_info_cache[key]
        td = tempfile.mkdtemp(prefix="clondisk_sw_")
        try:
            for name in ("SOFTWARE", "SOFTWARE.LOG1", "SOFTWARE.LOG2"):
                if os.path.exists(os.path.join(cfg, name)):
                    shutil.copyfile(os.path.join(cfg, name), os.path.join(td, name))
            with self._loaded_hive(os.path.join(td, "SOFTWARE"), "CLONDISK_SW", lambda *a: None) as root:
                info = self._read_version_key(winreg.HKEY_LOCAL_MACHINE,
                                              rf"{root}\Microsoft\Windows NT\CurrentVersion")
        finally:
            shutil.rmtree(td, ignore_errors=True)
        self._win_info_cache[key] = info
        return info

    def host_build(self) -> Optional[int]:
        try:
            return int(sys.getwindowsversion().build)
        except Exception:
            return None

    def has_mbr2gpt(self) -> bool:
        return os.path.exists(os.path.join(_system32(), "mbr2gpt.exe"))

    # ------------------------------------------------------------------ salud del disco
    def disk_health(self, disk: Disk) -> Optional[DiskHealth]:
        script = f"""
$pd = Get-PhysicalDisk | Where-Object {{ [string]$_.DeviceId -eq '{disk.number}' }} | Select-Object -First 1
if ($pd) {{
  $r = $null
  try {{ $r = $pd | Get-StorageReliabilityCounter -ErrorAction Stop }} catch {{}}
  [pscustomobject]@{{
    Health = [string]$pd.HealthStatus
    ReadErr = $(if ($r -and $r.ReadErrorsUncorrected -ne $null) {{ [int64]$r.ReadErrorsUncorrected }} else {{ $null }})
    Temp = $(if ($r -and $r.Temperature -ne $null) {{ [int64]$r.Temperature }} else {{ $null }})
    Wear = $(if ($r -and $r.Wear -ne $null) {{ [int64]$r.Wear }} else {{ $null }})
  }} | ConvertTo-Json -Compress
}}
"""
        data = run_ps_json(script, timeout=120)
        if not data:
            return None
        h = DiskHealth(health_status=data.get("Health") or None,
                       read_errors_uncorrected=data.get("ReadErr"),
                       temperature=data.get("Temp"), wear=data.get("Wear"))
        h.counters_available = any(v is not None for v in (h.read_errors_uncorrected,
                                                           h.temperature, h.wear))
        return h

    # ------------------------------------------------------------------ Windows sin conexión
    @staticmethod
    def _require_offline_letter(part: Partition) -> str:
        if part.is_boot:
            raise CommandError(f"{part.display_name()} es el Windows en ejecución")
        if not part.letter:
            raise CommandError(f"{part.display_name()} no tiene letra de unidad asignada")
        host = os.environ.get("SystemDrive", "C:")[:1].upper()
        if part.letter.upper() == host:
            raise CommandError(f"{part.letter}: es la unidad del sistema de este equipo")
        return part.letter.upper()

    def backup_drivers(self, part: Partition, log: LogFn) -> DriverBackup:
        """Exporta los drivers de terceros del Windows sin conexión. Si DISM falla (p. ej.
        porque ese Windows es más nuevo que el anfitrión) copia las carpetas del DriverStore
        que corresponden a los oem*.inf."""
        letter = self._require_offline_letter(part)
        docs = _documents_dir()
        # La carpeta destino se crea completa ANTES de llamar a DISM.
        dest, fallback = driver_backup_dir(docs, time.strftime("%Y%m%d_%H%M%S"))
        if fallback:
            log(f"No se pudo crear la carpeta en Documentos ({docs or 'desconocida'}); suele "
                "deberse al «Acceso controlado a carpetas» de Windows Defender. El respaldo se "
                f"guarda en {dest}", "warn")
        dism = os.path.join(_system32(), "dism.exe")
        cmd = f'"{dism}" /English /Image:{letter}:\\ /Export-Driver /Destination:"{dest}"'
        r = subprocess.run(cmd, capture_output=True, timeout=3600, creationflags=CREATE_NO_WINDOW)
        out = (r.stdout + r.stderr).decode("oem", "replace").strip()
        infs = count_inf_files(dest) if r.returncode == 0 else 0
        if infs:
            log(f"DISM exportó {infs} driver(s) (.inf) a {dest}")
            return DriverBackup(dest, infs, "dism", used_fallback_dir=fallback)
        if r.returncode == 0:
            log("DISM terminó sin errores pero no exportó ningún .inf", "warn")
        else:
            log(f"DISM /export-driver falló (código {r.returncode}): {out[-600:]}", "warn")
        log("Se usa el respaldo alternativo: copia de carpetas del DriverStore", "warn")
        backup = self._backup_driverstore(letter, dest, log)
        backup.used_fallback_dir = fallback
        return backup

    def _backup_driverstore(self, letter: str, dest: str, log: LogFn) -> DriverBackup:
        inf_dir = f"{letter}:\\Windows\\INF"
        repo = f"{letter}:\\Windows\\System32\\DriverStore\\FileRepository"
        oem: Dict[str, str] = {}
        sizes = set()
        for name in os.listdir(inf_dir):
            if re.fullmatch(r"oem\d+\.inf", name, re.IGNORECASE):
                path = os.path.join(inf_dir, name)
                oem[name] = _file_digest(path)
                sizes.add(os.path.getsize(path))
        repo_digests: Dict[str, List[str]] = {}
        for folder in os.listdir(repo):
            fp = os.path.join(repo, folder)
            if not os.path.isdir(fp):
                continue
            digests = []
            for f in os.listdir(fp):
                path = os.path.join(fp, f)
                if f.lower().endswith(".inf") and os.path.getsize(path) in sizes:
                    digests.append(_file_digest(path))
            repo_digests[folder] = digests
        found, missing = match_oem_driver_folders(oem, repo_digests)
        for folder in sorted(set(found.values())):
            shutil.copytree(os.path.join(repo, folder), os.path.join(dest, folder), dirs_exist_ok=True)
        with open(os.path.join(dest, "indice_oem.txt"), "w", encoding="utf-8") as f:
            for name, folder in found.items():
                f.write(f"{name}\t{folder}\n")
            for name in missing:
                f.write(f"{name}\t(no encontrado en FileRepository)\n")
        count = len(set(found.values()))
        if missing:
            log(f"{len(missing)} oem*.inf sin carpeta en el DriverStore: {', '.join(missing)}", "warn")
        if not oem:
            raise CommandError(f"No hay drivers de terceros (oem*.inf) en {inf_dir}")
        if not count:
            raise CommandError("No se pudo emparejar ningún oem*.inf con el DriverStore")
        log(f"Copiadas {count} carpeta(s) de drivers del DriverStore a {dest}")
        return DriverBackup(dest, count, "driverstore", missing)

    def prepare_offline_volume(self, disk: Disk, part: Partition, log: LogFn) -> List[str]:
        """Borra hiberfil/pagefile/swapfile y las instantáneas del volumen de un Windows
        que NO está en ejecución, para que Windows pueda reducirlo más."""
        letter = self._require_offline_letter(part)
        done: List[str] = []
        for name in ("hiberfil.sys", "pagefile.sys", "swapfile.sys"):
            path = f"{letter}:\\{name}"
            if not os.path.exists(path):
                continue
            size = os.path.getsize(path)
            run_ps(f"Remove-Item -LiteralPath {ps_str(path)} -Force")
            done.append(f"Eliminado {path} ({fmt_size(size)})")
        r = subprocess.run([os.path.join(_system32(), "vssadmin.exe"), "delete", "shadows",
                            f"/for={letter}:", "/quiet"],
                           capture_output=True, timeout=600, creationflags=CREATE_NO_WINDOW)
        out = (r.stdout + r.stderr).decode("oem", "replace").strip()
        done.append(f"Instantáneas de {letter}: eliminadas" if r.returncode == 0 else
                    f"Instantáneas de {letter}: ninguna eliminada ({out or r.returncode})")
        for d in done:
            log(d)
        return done

    def open_disk(self, disk: Disk, write: bool = False) -> Device:
        return WinDevice(rf"\\.\PhysicalDrive{disk.number}", write=write,
                         sector_size=disk.sector_size)

    # ------------------------------------------------------------------
    def _partition_number(self, disk_no: int, offset: int) -> int:
        out = run_ps(f"(Get-Partition -DiskNumber {disk_no} | Where-Object {{ $_.Offset -eq {offset} }}"
                     ").PartitionNumber").strip()
        if not out:
            raise CommandError(f"No se encontró la partición en el offset {offset} del disco {disk_no}")
        return int(out.splitlines()[0])

    def _update_properties(self, disk: Disk):
        try:
            with WinDevice(rf"\\.\PhysicalDrive{disk.number}", sector_size=disk.sector_size) as dev:
                dev.ioctl(IOCTL_DISK_UPDATE_PROPERTIES, check=False)
        except Exception:
            pass

    def prepare_target(self, disk: Disk, log: LogFn) -> None:
        n = disk.number
        info = run_ps_json(f"Get-Disk -Number {n} | Select-Object IsBoot,IsSystem,IsReadOnly,IsOffline "
                           "| ConvertTo-Json -Compress")
        if info.get("IsBoot") or info.get("IsSystem"):
            raise CommandError("El destino es el disco del sistema en ejecución")
        if info.get("IsReadOnly"):
            run_ps(f"Set-Disk -Number {n} -IsReadOnly $false")
            log("Se quitó el atributo de solo lectura del destino")
        if not info.get("IsOffline"):
            # Bloquear y desmontar los volúmenes mientras aún están montados; los handles
            # quedan abiertos hasta bring_online() para que Windows no los vuelva a montar.
            self._lock_volumes(disk, log)
            try:
                run_ps(f"Set-Disk -Number {n} -IsOffline $true")
                log(f"Disco {n} desconectado (offline) para escritura directa")
            except CommandError as e:
                log(f"No se pudo poner offline el disco ({e}); sus volúmenes siguen bloqueados", "warn")
        self._delete_layout(disk, log)

    def _delete_layout(self, disk: Disk, log: LogFn):
        """Borra la tabla de particiones del destino para que Windows no tenga volúmenes en él."""
        try:
            with WinDevice(rf"\\.\PhysicalDrive{disk.number}", write=True,
                           sector_size=disk.sector_size) as dev:
                dev.ioctl(IOCTL_DISK_DELETE_DRIVE_LAYOUT)
                dev.ioctl(IOCTL_DISK_UPDATE_PROPERTIES, check=False)
            log(f"Tabla de particiones del Disco {disk.number} eliminada (IOCTL_DISK_DELETE_DRIVE_LAYOUT)")
        except DeviceError as e:
            log(f"No se pudo borrar el layout con IOCTL ({e}); se sobrescribirá al copiar", "warn")

    def write_denied_diagnosis(self, disk: Disk) -> str:
        try:
            out = run_ps("(Get-MpPreference).EnableControlledFolderAccess", timeout=60).strip()
            cfa = int(out.splitlines()[-1]) if out else None
        except (CommandError, ValueError):
            cfa = None
        return write_denied_advice(cfa, sys.executable)

    def _lock_volumes(self, disk: Disk, log: LogFn):
        for p in disk.partitions:
            target = None
            if p.letter:
                target = rf"\\.\{p.letter}:"
            elif p.volume_path:
                target = p.volume_path.rstrip("\\")
            if not target:
                continue
            try:
                dev = WinDevice(target, write=True, sector_size=disk.sector_size)
            except DeviceError as e:
                log(f"No se pudo abrir {p.display_name()} para bloquearlo: {e}", "warn")
                continue
            locked = True
            try:
                dev.ioctl(FSCTL_LOCK_VOLUME)
            except DeviceError:
                locked = False  # hay archivos abiertos: se fuerza el desmontaje
            try:
                dev.ioctl(FSCTL_DISMOUNT_VOLUME)
            except DeviceError as e:
                dev.close()
                self._unlock_volumes()
                raise CommandError(f"No se pudo desmontar {p.display_name()} del destino: {e}. "
                                   "Cierra los programas que lo usen e inténtalo de nuevo.")
            self._locked_volumes.append(dev)
            log(f"Volumen {p.display_name()} {'bloqueado y ' if locked else ''}desmontado"
                + ("" if locked else " a la fuerza (tenía archivos abiertos)"),
                "info" if locked else "warn")

    def _unlock_volumes(self):
        for dev in self._locked_volumes:
            dev.close()
        self._locked_volumes.clear()

    def refresh_target(self, disk: Disk, log: LogFn) -> None:
        self._update_properties(disk)

    def bring_online(self, disk: Disk, log: LogFn) -> None:
        n = disk.number
        # Releer la tabla nueva ANTES de soltar los bloqueos, para que Windows no
        # vuelva a montar los volúmenes antiguos en sus offsets anteriores.
        self._update_properties(disk)
        self._unlock_volumes()
        run_ps(f"Set-Disk -Number {n} -IsOffline $false; Set-Disk -Number {n} -IsReadOnly $false")
        self._update_properties(disk)
        run_ps(f"Update-Disk -Number {n}")
        for _ in range(30):
            cnt = run_ps(f"@(Get-Partition -DiskNumber {n} -ErrorAction SilentlyContinue).Count").strip()
            if cnt and int(cnt) > 0:
                break
            time.sleep(1)
        time.sleep(2)  # dar tiempo al montaje automático de volúmenes
        log(f"Disco {n} conectado (online)")

    def set_offline(self, disk: Disk, log: LogFn) -> None:
        run_ps(f"Set-Disk -Number {disk.number} -IsOffline $true")
        log(f"Disco {disk.number} desconectado (offline)")

    # ------------------------------------------------------------------
    def create_snapshot(self, part: Partition, sector_size: int, log: LogFn):
        vol = f"{part.letter}:\\" if part.letter else part.volume_path
        if not vol.endswith("\\"):
            vol += "\\"
        script = f"""
$r = Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create -Arguments @{{Volume={ps_str(vol)}; Context='ClientAccessible'}}
if ($r.ReturnValue -ne 0) {{ throw "VSS:$($r.ReturnValue)" }}
$s = Get-CimInstance -ClassName Win32_ShadowCopy | Where-Object {{ $_.ID -eq $r.ShadowID }}
[pscustomobject]@{{ Id = [string]$r.ShadowID; Device = [string]$s.DeviceObject }} | ConvertTo-Json -Compress
"""
        try:
            info = run_ps_json(script)
        except CommandError as e:
            msg = str(e)
            m = re.search(r"VSS:(\d+)", msg)
            if m:
                code = int(m.group(1))
                msg = VSS_ERRORS.get(code, f"código {code}")
            raise CommandError(
                f"No se pudo crear la instantánea VSS de {part.display_name()} ({msg}). "
                "Comprueba que el servicio «Instantáneas de volumen» esté habilitado y que haya "
                "espacio libre suficiente.") from None
        dev = WinDevice(info["Device"], sector_size=sector_size, extended_dasd=True)
        if not dev.extended_dasd:
            log("No se pudo habilitar la lectura extendida en la instantánea; el sector de "
                "arranque de respaldo de NTFS podría no leerse", "warn")
        log(f"VSS {info['Id']} → {info['Device']}")
        return info["Id"], dev

    def delete_snapshot(self, snap_id: str, log: LogFn) -> None:
        run_ps("Get-CimInstance -ClassName Win32_ShadowCopy | Where-Object { $_.ID -eq "
               f"{ps_str(snap_id)} }} | Remove-CimInstance")
        log(f"Instantánea {snap_id} eliminada")

    # ------------------------------------------------------------------
    def extend_filesystem(self, disk: Disk, part_offset: int, log: LogFn) -> None:
        pn = self._partition_number(disk.number, part_offset)
        try:
            run_diskpart([f"select disk {disk.number}", f"select partition {pn}", "extend filesystem"])
        except CommandError:
            # Alternativa: dar foco al volumen mediante una letra temporal.
            letter, tmp, _ = self._ensure_letter(disk, part_offset)
            try:
                run_diskpart([f"select volume {letter}", "extend filesystem"])
            finally:
                if tmp:
                    self._remove_letter(disk, pn, letter, log)
        sizes = run_ps_json(
            f"$p = Get-Partition -DiskNumber {disk.number} -PartitionNumber {pn}; $v = $p | Get-Volume; "
            "[pscustomobject]@{P=[uint64]$p.Size; V=[uint64]$v.Size} | ConvertTo-Json -Compress")
        if sizes and sizes["V"] < sizes["P"] * 0.98:
            raise CommandError("El volumen no alcanzó el tamaño de la partición")
        log(f"Volumen ampliado (partición {pn})")

    def _free_letter(self) -> str:
        mask = _K32.get().GetLogicalDrives()
        for ch in "ZYXWVUTSRQPONMLKJIHGFED":
            if not mask & (1 << (ord(ch) - ord("A"))):
                return ch
        raise CommandError("No hay letras de unidad libres")

    def _ensure_letter(self, disk: Disk, offset: int) -> Tuple[str, bool, int]:
        """Devuelve (letra, asignada_por_nosotros, número_de_partición)."""
        pn = self._partition_number(disk.number, offset)
        cur = run_ps(f"$l = (Get-Partition -DiskNumber {disk.number} -PartitionNumber {pn}).DriveLetter; "
                     "if ($l -and [int][char]$l -ne 0) { [string]$l }").strip()
        if cur:
            return cur[0], False, pn
        letter = self._free_letter()
        try:
            run_ps(f"Add-PartitionAccessPath -DiskNumber {disk.number} -PartitionNumber {pn} "
                   f"-AccessPath '{letter}:\\'")
        except CommandError:
            run_diskpart([f"select disk {disk.number}", f"select partition {pn}", f"assign letter={letter}"])
        for _ in range(20):
            if os.path.exists(f"{letter}:\\"):
                break
            time.sleep(0.5)
        return letter, True, pn

    def _remove_letter(self, disk: Disk, pn: int, letter: str, log: LogFn):
        try:
            run_diskpart([f"select disk {disk.number}", f"select partition {pn}",
                          f"remove letter={letter}"])
        except CommandError as e:
            log(f"No se pudo quitar la letra {letter}: ({e})", "warn")

    def make_bootable(self, disk: Disk, windows_offset: int, system_offset: int,
                      firmware: str, log: LogFn, win_build: Optional[int] = None) -> None:
        win, win_tmp, win_pn = self._ensure_letter(disk, windows_offset)
        sysl, sys_tmp, sys_pn = (win, False, win_pn) if system_offset == windows_offset else \
            self._ensure_letter(disk, system_offset)
        try:
            if not os.path.exists(f"{win}:\\Windows\\System32\\winload.efi") and \
                    not os.path.exists(f"{win}:\\Windows\\System32\\winload.exe"):
                raise CommandError(f"No se encontró Windows en {win}:")
            host_exe = os.path.join(_system32(), "bcdboot.exe")
            target_exe = f"{win}:\\Windows\\System32\\bcdboot.exe"
            exe, why = choose_bcdboot(self.host_build(), win_build, host_exe, target_exe,
                                      os.path.exists(target_exe))
            log(f"bcdboot: {why} → {exe}")
            args = [f"{win}:\\Windows", "/s", f"{sysl}:", "/f", firmware]
            if firmware == "UEFI":
                args.append("/addlast")  # no cambia el orden de arranque actual
            try:
                out = run_exe([exe] + args)
            except CommandError as e:
                if exe == host_exe:
                    raise
                log(f"El bcdboot del Windows clonado falló ({e}); se reintenta con el del "
                    f"anfitrión: {host_exe}", "warn")
                out = run_exe([host_exe] + args)
            log(f"bcdboot: {out.strip()}")
        finally:
            if sys_tmp:
                self._remove_letter(disk, sys_pn, sysl, log)
            if win_tmp:
                self._remove_letter(disk, win_pn, win, log)

    @contextlib.contextmanager
    def _loaded_hive(self, hive_file: str, hive_name: str, log: LogFn):
        """Carga ``hive_file`` en ``HKLM\\<hive_name>`` y la descarga al salir."""
        reg = os.path.join(_system32(), "reg.exe")
        try:  # restos de una ejecución anterior interrumpida
            run_exe([reg, "unload", f"HKLM\\{hive_name}"])
        except CommandError:
            pass
        run_exe([reg, "load", f"HKLM\\{hive_name}", hive_file])
        try:
            yield hive_name
        finally:
            gc.collect()
            for attempt in range(5):
                try:
                    run_exe([reg, "unload", f"HKLM\\{hive_name}"])
                    break
                except CommandError:
                    time.sleep(1)
            else:
                log(f"No se pudo descargar HKLM\\{hive_name}; reinicia antes de usar el SSD", "warn")

    @contextlib.contextmanager
    def _target_system_hive(self, disk: Disk, windows_offset: int, log: LogFn):
        win, win_tmp, win_pn = self._ensure_letter(disk, windows_offset)
        try:
            hive = f"{win}:\\Windows\\System32\\config\\SYSTEM"
            with self._loaded_hive(hive, "CLONDISK_SYS", log) as root:
                yield root
        finally:
            if win_tmp:
                self._remove_letter(disk, win_pn, win, log)

    def remap_mounted_devices(self, disk: Disk, windows_offset: int,
                              mapping: List[Tuple[bytes, bytes]], log: LogFn) -> None:
        import winreg
        with self._target_system_hive(disk, windows_offset, log) as root:
            changed = 0
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{root}\MountedDevices", 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
                values = []
                i = 0
                while True:
                    try:
                        values.append(winreg.EnumValue(key, i))
                    except OSError:
                        break
                    i += 1
                lookup: Dict[bytes, bytes] = dict(mapping)
                for name, data, typ in values:
                    if typ == winreg.REG_BINARY and bytes(data) in lookup:
                        winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, lookup[bytes(data)])
                        changed += 1
        log(f"MountedDevices del Windows clonado actualizado ({changed} entradas)")

    def prepare_new_hardware(self, disk: Disk, windows_offset: int, log: LogFn) -> None:
        with self._target_system_hive(disk, windows_offset, log) as root:
            changes = enable_boot_storage_drivers(WinregHive(root), log)
        if not changes:
            log("Los drivers de almacenamiento de arranque ya estaban habilitados")

    # ------------------------------------------------------------------ MBR → GPT
    def _mbr2gpt_logs(self, max_lines: int = 60) -> List[str]:
        windir = os.environ.get("SystemRoot", r"C:\Windows")
        lines: List[str] = []
        for name in ("setupact.log", "setuperr.log"):
            path = os.path.join(windir, name)
            try:
                with open(path, "rb") as f:
                    text = _decode_log(f.read())
            except OSError:
                continue
            tail = [ln for ln in text.splitlines() if ln.strip()][-max_lines:]
            lines.append(f"--- {path} (últimas {len(tail)} líneas) ---")
            lines += tail
        return lines

    def convert_to_gpt(self, disk: Disk, log: LogFn) -> None:
        exe = os.path.join(_system32(), "mbr2gpt.exe")
        if not os.path.exists(exe):
            raise CommandError("No se encontró mbr2gpt.exe (requiere Windows 10 1703 o posterior)")
        for action in ("/validate", "/convert"):
            r = subprocess.run([exe, action, f"/disk:{disk.number}", "/allowFullOS"],
                               capture_output=True, timeout=1800, creationflags=CREATE_NO_WINDOW)
            out = (r.stdout + r.stderr).decode("oem", "replace").strip()
            log(f"mbr2gpt {action}: {out}")
            if r.returncode != 0:
                for line in self._mbr2gpt_logs():
                    log(line, "warn")
                raise CommandError(f"mbr2gpt {action} falló (código {r.returncode})")
        self._update_properties(disk)
        run_ps(f"Update-Disk -Number {disk.number}")
        log(f"Disco {disk.number} convertido a GPT")

    # ------------------------------------------------------------------ comprobación del arranque
    def boot_file_checks(self, disk: Disk, style: str, system_offset: int, windows_offset: int,
                         log: LogFn, check_bcd: bool = True) -> List[Tuple[bool, str]]:
        checks: List[Tuple[bool, str]] = []
        sysl, sys_tmp, sys_pn = self._ensure_letter(disk, system_offset)
        win, win_tmp, win_pn = (sysl, False, sys_pn) if windows_offset == system_offset else \
            self._ensure_letter(disk, windows_offset)
        try:
            if style == "MBR":
                bcd = f"{sysl}:\\Boot\\BCD"
                checks.append((os.path.exists(f"{sysl}:\\bootmgr"), "bootmgr en la partición de sistema"))
                checks.append((os.path.exists(bcd), "Boot\\BCD en la partición de sistema"))
            else:
                bcd = f"{sysl}:\\EFI\\Microsoft\\Boot\\BCD"
                checks.append((os.path.exists(bcd), "\\EFI\\Microsoft\\Boot\\BCD en la partición EFI"))
                checks.append((os.path.exists(f"{sysl}:\\EFI\\Boot\\bootx64.efi"),
                               "\\EFI\\Boot\\bootx64.efi en la partición EFI"))
            if check_bcd and os.path.exists(bcd):
                try:
                    out = run_exe([os.path.join(_system32(), "bcdedit.exe"), "/store", bcd,
                                   "/enum", "{default}"])
                    letters = bcdedit_partition_letters(out)
                    ok = letters == {win.upper()}
                    found = ", ".join(f"{x}:" for x in sorted(letters)) or "ninguna partición"
                    checks.append((ok, f"BCD {{default}} apunta a la partición de Windows del destino "
                                       f"({win}:)" + ("" if ok else f" — apunta a: {found}")))
                except CommandError as e:
                    checks.append((False, f"bcdedit no pudo leer el BCD: {e}"))
        finally:
            if win_tmp:
                self._remove_letter(disk, win_pn, win, log)
            if sys_tmp:
                self._remove_letter(disk, sys_pn, sysl, log)
        return checks

    def retrim(self, disk: Disk, log: LogFn) -> None:
        run_ps(f"Get-Partition -DiskNumber {disk.number} | Get-Volume -ErrorAction SilentlyContinue | "
               "Where-Object { $_.DriveLetter -and $_.FileSystem -eq 'NTFS' } | "
               "ForEach-Object { Optimize-Volume -DriveLetter $_.DriveLetter -ReTrim }", timeout=1800)
        log("TRIM enviado al SSD")

    # ------------------------------------------------------------------
    def min_partition_size(self, disk: Disk, part: Partition) -> Optional[int]:
        pn = self._partition_number(disk.number, part.offset_bytes(disk.sector_size))
        out = run_ps(f"(Get-PartitionSupportedSize -DiskNumber {disk.number} -PartitionNumber {pn}).SizeMin")
        return int(out.strip())

    def shrink_partition(self, disk: Disk, part: Partition, new_size: int, log: LogFn) -> None:
        pn = self._partition_number(disk.number, part.offset_bytes(disk.sector_size))
        run_ps(f"Resize-Partition -DiskNumber {disk.number} -PartitionNumber {pn} -Size {int(new_size)}",
               timeout=3600)
        log(f"Partición {part.display_name()} reducida a {new_size} bytes")
