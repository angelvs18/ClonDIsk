"""Decisiones sobre el Windows clonado, independientes del sistema operativo.

Aquí está la lógica que se puede probar sin discos reales ni ``winreg``:

* salud del disco de origen (contadores de fiabilidad);
* si se puede convertir el destino a GPT/UEFI con ``mbr2gpt``;
* qué ``bcdboot.exe`` usar (el del anfitrión o el del Windows clonado);
* qué carpetas del DriverStore corresponden a los ``oem*.inf`` (respaldo de drivers);
* habilitar los drivers de almacenamiento de arranque en una colmena SYSTEM;
* interpretar la salida de ``bcdedit``;
* Acceso controlado a carpetas, actualizaciones pendientes, IP fijas, WinRE y la firma
  del gestor de arranque (Secure Boot).
"""
from __future__ import annotations

import json
import ntpath
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .model import Disk, Partition

MBR2GPT_MIN_HOST_BUILD = 15063     # Windows 10 1703
WIN10_FIRST_BUILD = 10240


# ---------------------------------------------------------------------------
# Salud del disco
# ---------------------------------------------------------------------------
@dataclass
class DiskHealth:
    health_status: Optional[str] = None        # Healthy | Warning | Unhealthy | ...
    read_errors_uncorrected: Optional[int] = None
    temperature: Optional[int] = None          # °C
    wear: Optional[int] = None                 # % de desgaste (SSD)
    counters_available: bool = False


def assess_health(h: Optional[DiskHealth]) -> Tuple[str, List[str]]:
    """Devuelve (nivel, mensajes). Nivel: ``ok`` | ``danger`` | ``unknown``."""
    if h is None or (h.health_status is None and not h.counters_available):
        return "unknown", ["No se pudo consultar la salud del disco (es habitual con algunos "
                           "adaptadores USB). No se bloquea nada, pero no hay garantías."]
    msgs: List[str] = []
    level = "ok"
    status = (h.health_status or "").strip()
    if status and status.lower() != "healthy":
        level = "danger"
        msgs.append(f"Windows informa que el disco NO está sano (estado: {status}).")
    if h.read_errors_uncorrected:
        level = "danger"
        msgs.append(f"El disco tiene {h.read_errors_uncorrected} error(es) de lectura no corregidos.")
    if not h.counters_available:
        msgs.append("Los contadores de fiabilidad (errores, temperatura, desgaste) no están "
                    "disponibles (es habitual con adaptadores USB).")
    else:
        det = []
        if h.temperature:
            det.append(f"temperatura {h.temperature} °C")
        if h.wear is not None:
            det.append(f"desgaste {h.wear} %")
        det.append(f"errores de lectura no corregidos: {h.read_errors_uncorrected or 0}")
        msgs.append("Contadores: " + ", ".join(det) + ".")
    if level == "ok" and status:
        msgs.insert(0, "Estado del disco: Healthy.")
    return level, msgs


# ---------------------------------------------------------------------------
# Windows del origen
# ---------------------------------------------------------------------------
def source_windows(disk: Disk) -> Optional[Partition]:
    for p in disk.partitions:
        if p.has_windows or p.is_boot:
            return p
    return None


def offline_windows(disk: Disk) -> Optional[Partition]:
    """Partición con un Windows que NO es el que está en ejecución."""
    p = source_windows(disk)
    return p if p is not None and p.has_windows and not p.is_boot else None


# ---------------------------------------------------------------------------
# Conversión MBR → GPT (mbr2gpt)
# ---------------------------------------------------------------------------
def gpt_conversion_status(source: Disk, host_build: Optional[int],
                          has_mbr2gpt: bool) -> Tuple[bool, str]:
    """¿Se puede ofrecer la conversión a GPT/UEFI? Devuelve (permitida, motivo)."""
    if source.style != "MBR":
        return False, "El origen ya es GPT."
    if host_build is None or host_build < MBR2GPT_MIN_HOST_BUILD:
        hb = host_build if host_build is not None else "desconocido"
        return False, (f"Este equipo tiene Windows build {hb}; mbr2gpt necesita Windows 10 "
                       "1703 (build 15063) o posterior en el equipo que ejecuta ClonDisk.")
    if not has_mbr2gpt:
        return False, "No se encontró mbr2gpt.exe en System32 de este equipo."
    if len(source.partitions) > 3:
        return False, (f"El origen tiene {len(source.partitions)} particiones primarias; mbr2gpt "
                       "admite como máximo 3 (necesita un hueco para crear la partición EFI).")
    win = source_windows(source)
    legacy = ("El SSD tendrá que arrancar en modo BIOS/Legacy (CSM): el equipo de destino "
              "debe tener Legacy/CSM habilitado.")
    if win is None:
        return False, "No se detectó Windows en el origen. " + legacy
    info = win.win_info
    if info is None or info.build is None:
        return False, "No se pudo determinar la versión del Windows de origen. " + legacy
    if not info.is_win10_or_later:
        return False, (f"El Windows de origen ({info.display()}) es anterior a Windows 10 y no "
                       "admite conversión con mbr2gpt. " + legacy)
    return True, ""


# ---------------------------------------------------------------------------
# bcdboot
# ---------------------------------------------------------------------------
def choose_bcdboot(host_build: Optional[int], win_build: Optional[int], host_exe: str,
                   target_exe: str, target_exists: bool) -> Tuple[str, str]:
    """Elige el ``bcdboot.exe``: el del Windows clonado si es más nuevo que el anfitrión."""
    if (win_build is not None and host_build is not None and win_build > host_build
            and target_exists):
        return target_exe, (f"se usa el bcdboot del Windows clonado (build {win_build} > "
                            f"anfitrión {host_build})")
    if win_build is not None and host_build is not None and win_build > host_build:
        return host_exe, (f"el Windows clonado (build {win_build}) es más nuevo que el anfitrión "
                          f"({host_build}) pero no tiene bcdboot.exe; se usa el del anfitrión")
    return host_exe, f"se usa el bcdboot del anfitrión (build {host_build or '?'})"


# ---------------------------------------------------------------------------
# Respaldo de drivers (alternativa a DISM)
# ---------------------------------------------------------------------------
@dataclass
class DriverBackup:
    folder: str
    count: int
    method: str                    # "dism" | "driverstore"
    unmatched: List[str] = field(default_factory=list)
    used_fallback_dir: bool = False   # no se pudo usar Documentos


DRIVER_BACKUP_FALLBACK_ROOT = "C:\\ClonDisk"


def driver_backup_dir(documents: Optional[str], stamp: str,
                      fallback_root: str = DRIVER_BACKUP_FALLBACK_ROOT,
                      makedirs: Callable[..., None] = os.makedirs) -> Tuple[str, bool]:
    """Crea ``<Documentos>\\ClonDisk\\drivers_<stamp>`` y, si no se puede (o no se conoce
    la carpeta Documentos), ``<fallback_root>\\drivers_<stamp>``.
    Devuelve (ruta creada, se_usó_la_alternativa)."""
    if documents:
        path = os.path.join(documents, "ClonDisk", f"drivers_{stamp}")
        try:
            makedirs(path, exist_ok=True)
            if os.path.isdir(path):
                return path, False
        except OSError:
            pass
    path = os.path.join(fallback_root, f"drivers_{stamp}")
    makedirs(path, exist_ok=True)
    return path, True


def count_inf_files(folder: str) -> int:
    """Número de archivos .inf bajo ``folder`` (DISM exporta un subdirectorio por driver)."""
    return sum(1 for _root, _dirs, files in os.walk(folder)
               for f in files if f.lower().endswith(".inf"))


def match_oem_driver_folders(oem: Dict[str, str],
                             repo: Dict[str, Iterable[str]]) -> Tuple[Dict[str, str], List[str]]:
    """Empareja cada ``oemNN.inf`` (nombre → huella del contenido) con la carpeta de
    ``FileRepository`` que contiene un .inf idéntico. Devuelve (oem → carpeta, sin pareja)."""
    by_digest: Dict[str, str] = {}
    for folder in sorted(repo):
        for digest in repo[folder]:
            by_digest.setdefault(digest, folder)
    found: Dict[str, str] = {}
    missing: List[str] = []
    for name in sorted(oem, key=str.lower):
        folder = by_digest.get(oem[name])
        if folder:
            found[name] = folder
        else:
            missing.append(name)
    return found, missing


# ---------------------------------------------------------------------------
# Drivers de almacenamiento de arranque (otra computadora)
# ---------------------------------------------------------------------------
BOOT_STORAGE_SERVICES = ("storahci", "stornvme", "iaStorV", "iaStorAVC")


class Hive:
    """Interfaz mínima sobre una colmena de registro cargada (rutas relativas a la raíz)."""

    def key_exists(self, path: str) -> bool:  # pragma: no cover - interfaz
        raise NotImplementedError

    def get_value(self, path: str, name: str):  # pragma: no cover - interfaz
        raise NotImplementedError

    def set_dword(self, path: str, name: str, value: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete_key(self, path: str) -> None:  # pragma: no cover - interfaz (recursivo)
        raise NotImplementedError

    def subkeys(self, path: str) -> List[str]:  # pragma: no cover - interfaz
        raise NotImplementedError


def current_control_set(hive: Hive) -> str:
    current = hive.get_value("Select", "Current")
    if not isinstance(current, int) or current <= 0:
        raise ValueError("La colmena SYSTEM no tiene un valor Select\\Current válido")
    return f"ControlSet{current:03d}"


def enable_boot_storage_drivers(hive: Hive, log: Callable[[str], None] = lambda m: None) -> List[str]:
    """Pone Start=0 en los drivers de almacenamiento de arranque del ControlSet actual y
    borra sus ``StartOverride``. Evita INACCESSIBLE_BOOT_DEVICE con otra controladora.
    Registra el estado de cada servicio aunque no cambie; devuelve solo los cambios."""
    cs = current_control_set(hive)
    changes: List[str] = []
    for svc in BOOT_STORAGE_SERVICES:
        key = f"{cs}\\Services\\{svc}"
        if not hive.key_exists(key):
            log(f"{svc}: no está instalado en este Windows (no existe {key})")
            continue
        state: List[str] = []
        old = hive.get_value(key, "Start")
        if old != 0:
            hive.set_dword(key, "Start", 0)
            changes.append(f"{key}: Start {old} → 0")
            state.append(f"Start {old} → 0")
        else:
            state.append("Start ya era 0")
        if hive.key_exists(key + "\\StartOverride"):
            hive.delete_key(key + "\\StartOverride")
            changes.append(f"{key}\\StartOverride eliminado")
            state.append("StartOverride eliminado")
        else:
            state.append("sin StartOverride")
        log(f"{svc}: " + ", ".join(state))
    return changes


# ---------------------------------------------------------------------------
# Actualizaciones pendientes en el Windows de origen
# ---------------------------------------------------------------------------
PENDING_UPDATES_WARNING = (
    "El Windows de origen tiene actualizaciones pendientes. El primer arranque puede quedarse "
    "en negro o mostrar «Deshaciendo cambios»; no lo apagues. Lo ideal es arrancar el origen "
    "una vez en su equipo para terminar las actualizaciones antes de clonar.")

_CBS = "Microsoft\\Windows\\CurrentVersion\\Component Based Servicing"
_PENDING_SOFTWARE_KEYS = [
    (_CBS + "\\RebootPending", "Component Based Servicing\\RebootPending"),
    (_CBS + "\\SessionsPending", "Component Based Servicing\\SessionsPending"),
    ("Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired",
     "WindowsUpdate\\Auto Update\\RebootRequired"),
]


def pending_update_reasons(software: Optional[Hive], system: Optional[Hive],
                           windows_dir: Optional[str]) -> List[str]:
    """Indicios de actualizaciones a medio instalar. ``software``/``system``: colmenas del
    Windows de origen (rutas relativas a su raíz); ``windows_dir``: su carpeta Windows."""
    reasons: List[str] = []
    if windows_dir and os.path.exists(os.path.join(windows_dir, "WinSxS", "pending.xml")):
        reasons.append("existe Windows\\WinSxS\\pending.xml")
    if software is not None:
        for key, label in _PENDING_SOFTWARE_KEYS:
            if software.key_exists(key):
                reasons.append(f"existe la clave {label}")
    if system is not None:
        try:
            cs = current_control_set(system)
        except ValueError:
            cs = None
        if cs:
            ops = system.get_value(f"{cs}\\Control\\Session Manager", "PendingFileRenameOperations")
            if isinstance(ops, str):
                ops = [ops]
            if ops and any(str(o).strip() for o in ops):
                reasons.append("Session Manager\\PendingFileRenameOperations tiene "
                               "archivos por reemplazar al reiniciar")
    return reasons


# ---------------------------------------------------------------------------
# Red con IP fija (dispositivos conectados directo, p. ej. un plotter por RJ45)
# ---------------------------------------------------------------------------
NET_ADAPTER_CLASS = "{4D36E972-E325-11CE-BFC1-08002BE10318}"
NETWORK_TXT_NAME = "ClonDisk - red anterior.txt"
NETWORK_PS1_NAME = "ClonDisk-restaurar-red.ps1"
_IPV4 = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$")


@dataclass
class StaticIp:
    interface: str                         # GUID de la interfaz en Tcpip\Parameters\Interfaces
    name: str = ""                         # nombre de la conexión («Ethernet 2»), si se conoce
    addresses: List[str] = field(default_factory=list)
    masks: List[str] = field(default_factory=list)
    gateways: List[str] = field(default_factory=list)
    dns: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or self.interface

    def prefix(self, i: int) -> int:
        masks = self.masks or ["255.255.255.0"]
        mask = masks[i] if i < len(masks) else masks[0]
        return sum(bin(int(octet)).count("1") for octet in mask.split("."))


def _ipv4_list(value) -> List[str]:
    """REG_MULTI_SZ (lista) o REG_SZ con valores separados por comas o espacios."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    return [str(x).strip() for x in items if _IPV4.match(str(x).strip())]


def read_static_ips(hive: Hive) -> List[StaticIp]:
    """Interfaces con IP fija (EnableDHCP=0) del ControlSet actual de una colmena SYSTEM."""
    cs = current_control_set(hive)
    base = f"{cs}\\Services\\Tcpip\\Parameters\\Interfaces"
    out: List[StaticIp] = []
    for guid in sorted(hive.subkeys(base), key=str.lower):
        key = f"{base}\\{guid}"
        if hive.get_value(key, "EnableDHCP") != 0:
            continue
        addrs = [a for a in _ipv4_list(hive.get_value(key, "IPAddress")) if a != "0.0.0.0"]
        if not addrs:
            continue
        name = hive.get_value(f"{cs}\\Control\\Network\\{NET_ADAPTER_CLASS}\\{guid}\\Connection",
                              "Name")
        out.append(StaticIp(guid, str(name or ""), addrs,
                            _ipv4_list(hive.get_value(key, "SubnetMask")),
                            [g for g in _ipv4_list(hive.get_value(key, "DefaultGateway"))
                             if g != "0.0.0.0"],
                            _ipv4_list(hive.get_value(key, "NameServer"))))
    return out


def fmt_static_ip(ip: StaticIp) -> str:
    addrs = ", ".join(f"{a}/{ip.masks[i] if i < len(ip.masks) else '?'}"
                      for i, a in enumerate(ip.addresses))
    text = f"{ip.label}: IP {addrs}"
    if ip.gateways:
        text += f" · puerta de enlace {', '.join(ip.gateways)}"
    if ip.dns:
        text += f" · DNS {', '.join(ip.dns)}"
    return text


def network_report_text(ips: List[StaticIp], stamp: str) -> str:
    lines = [f"ClonDisk — configuración de red con IP fija del equipo anterior ({stamp})", "",
             "El equipo de origen tenía estas tarjetas de red con IP fija. En el equipo nuevo la",
             "tarjeta es otra, así que Windows no le aplica esa IP. Si un dispositivo se conecta",
             "directo por cable (p. ej. un plotter por RJ45), pon la misma IP en la tarjeta nueva.", ""]
    for ip in ips:
        lines.append(f"Conexión: {ip.label}")
        lines.append(f"  Interfaz: {ip.interface}")
        for i, a in enumerate(ip.addresses):
            mask = ip.masks[i] if i < len(ip.masks) else (ip.masks[0] if ip.masks else "?")
            lines.append(f"  Dirección IP: {a}   Máscara de subred: {mask}")
        lines.append(f"  Puerta de enlace: {', '.join(ip.gateways) or '(ninguna)'}")
        lines.append(f"  DNS: {', '.join(ip.dns) or '(ninguno)'}")
        lines.append("")
    lines += ["Cómo aplicarla:",
              f"  - Automático: clic derecho en «{NETWORK_PS1_NAME}» (en este escritorio) →",
              "    «Ejecutar con PowerShell», elige la tarjeta nueva y acepta el aviso de administrador.",
              "  - A mano: Configuración → Red e Internet → Ethernet → Editar asignación de IP → Manual.",
              "",
              "Si Windows avisa que la IP ya está asignada a otro adaptador (el anterior, que ahora",
              "está oculto), puedes continuar; o quítalo en Administrador de dispositivos → Ver →",
              "Mostrar dispositivos ocultos → Adaptadores de red."]
    return "\r\n".join(lines) + "\r\n"


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def restore_network_script(ips: List[StaticIp], stamp: str) -> str:
    """Script de PowerShell que lista los adaptadores, pide elegir uno y aplica la IP fija."""
    def arr(values) -> str:
        return "@(" + ", ".join(_ps_quote(v) for v in values) + ")"

    configs = ",\n".join(
        f"    @{{ Nombre = {_ps_quote(ip.label)}; IP = {arr(ip.addresses)}; "
        f"Prefijo = @({', '.join(str(ip.prefix(i)) for i in range(len(ip.addresses)))}); "
        f"Puerta = {arr(ip.gateways)}; DNS = {arr(ip.dns)} }}" for ip in ips)
    return f"""# ClonDisk: restaurar la IP fija de la tarjeta de red del equipo anterior ({stamp})
# Clic derecho -> «Ejecutar con PowerShell». Pide permisos de administrador.
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}}
$configs = @(
{configs}
)
foreach ($c in $configs) {{
    Write-Host ""
    Write-Host ("Configuración anterior «{{0}}»: IP {{1}} · puerta de enlace {{2}} · DNS {{3}}" -f `
        $c.Nombre, ($c.IP -join ', '), ($c.Puerta -join ', '), ($c.DNS -join ', '))
    $ads = @(Get-NetAdapter | Sort-Object Name)
    for ($i = 0; $i -lt $ads.Count; $i++) {{
        Write-Host ("  {{0}}) {{1}} - {{2}} [{{3}}]" -f ($i + 1), $ads[$i].Name, $ads[$i].InterfaceDescription, $ads[$i].Status)
    }}
    $sel = Read-Host "Número del adaptador al que aplicar esta IP (Enter = omitir)"
    if (-not $sel) {{ continue }}
    $n = 0
    if (-not [int]::TryParse($sel, [ref]$n) -or $n -lt 1 -or $n -gt $ads.Count) {{
        Write-Host "Opción no válida; se omite." -ForegroundColor Yellow
        continue
    }}
    $ad = $ads[$n - 1]
    try {{
        Set-NetIPInterface -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
        Get-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
        Get-NetRoute -InterfaceIndex $ad.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
        for ($k = 0; $k -lt $c.IP.Count; $k++) {{
            $p = @{{ InterfaceIndex = $ad.ifIndex; IPAddress = $c.IP[$k]; PrefixLength = $c.Prefijo[$k]; AddressFamily = 'IPv4' }}
            if ($k -eq 0 -and $c.Puerta.Count -gt 0) {{ $p.DefaultGateway = $c.Puerta[0] }}
            New-NetIPAddress @p -ErrorAction Stop | Out-Null
        }}
        if ($c.DNS.Count -gt 0) {{
            Set-DnsClientServerAddress -InterfaceIndex $ad.ifIndex -ServerAddresses $c.DNS -ErrorAction Stop
        }}
        Write-Host "Listo: $($ad.Name) tiene ahora la IP $($c.IP -join ', ')" -ForegroundColor Green
    }} catch {{
        Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    }}
}}
Read-Host "Pulsa Enter para cerrar"
""".replace("\n", "\r\n")


# ---------------------------------------------------------------------------
# Entorno de recuperación (WinRE)
# ---------------------------------------------------------------------------
# reagentc /enable solo funciona con el Windows en ejecución: sin conexión se puede
# registrar la ubicación (/setreimage /target), pero WinRE sigue «Disabled» hasta que
# alguien ejecuta /enable en el primer arranque (Setup lo hace solo durante OOBE).
WINRE_ENABLE_HINT = ("WinRE queda registrado pero deshabilitado: en el primer arranque ejecuta "
                     "«reagentc /enable» como administrador y comprueba con «reagentc /info» "
                     "que diga Enabled.")


@dataclass
class WinreStatus:
    wim_folder: Optional[str] = None       # carpeta con Winre.wim (None = no se encontró)
    in_recovery_partition: bool = False
    registered: bool = False               # ReAgent.xml apunta a una ubicación de WinRE
    enabled: Optional[bool] = None         # InstallState de ReAgent.xml
    detail: str = ""


def find_winre_folder(candidates: Iterable[Tuple[str, bool]],
                      exists: Callable[[str], bool] = os.path.exists) -> Tuple[Optional[str], bool]:
    """Primera carpeta (de ``(carpeta, es_partición_de_recuperación)``) con Winre.wim."""
    for folder, is_recovery in candidates:
        if exists(os.path.join(folder, "Winre.wim")):
            return folder, is_recovery
    return None, False


def parse_reagent_xml(text: str) -> Dict[str, object]:
    """Datos útiles de ``Windows\\System32\\Recovery\\ReAgent.xml``."""
    def attr(tag: str, name: str) -> Optional[str]:
        m = re.search(rf"<{tag}\b[^>]*\b{name}\s*=\s*[\"']([^\"']*)[\"']", text or "", re.IGNORECASE)
        return m.group(1) if m else None

    state = attr("InstallState", "state")
    try:
        install_state = int(state) if state is not None else None
    except ValueError:
        install_state = None
    return {"winre_path": attr("WinreLocation", "path") or "",
            "image_path": attr("ImageLocation", "path") or "",
            "install_state": install_state}


def winre_status_from_xml(folder: Optional[str], in_recovery: bool, setreimage_ok: bool,
                          xml_text: Optional[str]) -> WinreStatus:
    info = parse_reagent_xml(xml_text or "")
    located = bool(info["winre_path"] or info["image_path"])
    state = info["install_state"]
    return WinreStatus(folder, in_recovery, bool(folder) and setreimage_ok and located,
                       None if state is None else state == 1)


def winre_check(st: WinreStatus) -> Tuple[bool, str]:
    """Línea ✓/✗ «WinRE registrado» para la comprobación final."""
    if not st.wim_folder:
        return False, ("WinRE registrado — no se encontró Winre.wim (ni en la partición de "
                       "recuperación ni en Windows\\System32\\Recovery)"
                       + (f": {st.detail}" if st.detail else ""))
    where = "partición de recuperación" if st.in_recovery_partition else "Windows\\System32\\Recovery"
    if not st.registered:
        return False, (f"WinRE registrado — Winre.wim está en {where}, pero reagentc no pudo "
                       "registrarlo" + (f": {st.detail}" if st.detail else ""))
    state = {True: "Enabled", False: "Disabled hasta el primer arranque",
             None: "estado desconocido"}[st.enabled]
    return True, f"WinRE registrado (Winre.wim en {where}; {state})"


# ---------------------------------------------------------------------------
# Secure Boot: firma del gestor de arranque UEFI
# ---------------------------------------------------------------------------
SECURE_BOOT_2023_WARNING = (
    "El gestor de arranque del destino (bootmgfw.efi) está firmado con el certificado nuevo "
    "«Windows UEFI CA 2023». Equipos con BIOS anterior a 2023 pueden rechazarlo con Secure "
    "Boot activo: actualiza la BIOS o desactiva Secure Boot.")
SECURE_BOOT_HELP = (
    "Si al arrancar aparece «Se ha detectado una firma no válida. Comprobar directiva de "
    "arranque seguro», actualiza la BIOS/UEFI del equipo (los firmwares anteriores a 2023 no "
    "conocen el certificado «Windows UEFI CA 2023») o desactiva Secure Boot en su configuración.")


def classify_boot_signature(issuer: Optional[str], chain: Iterable[str] = ()) -> str:
    """Certificado con el que se firmó bootmgfw.efi, a partir del emisor del firmante y de la
    cadena: ``"2023"`` (Windows UEFI CA 2023), ``"2011"`` (Microsoft Windows Production PCA
    2011), ``"unsigned"`` o ``"unknown"``."""
    texts = [issuer or ""] + [c or "" for c in chain]
    if not any(t.strip() for t in texts):
        return "unsigned"
    for text in texts:                       # el emisor directo manda sobre el resto de la cadena
        if re.search(r"UEFI CA 2023", text, re.IGNORECASE):
            return "2023"
        if re.search(r"Windows Production PCA 2011", text, re.IGNORECASE):
            return "2011"
    return "unknown"


# ---------------------------------------------------------------------------
# Acceso controlado a carpetas (Windows Defender)
# ---------------------------------------------------------------------------
# EnableControlledFolderAccess: 0 desactivado, 1 activado, 2 auditoría,
# 3 bloquear solo la modificación de discos, 4 auditar la modificación de discos.
CFA_BLOCKING_MODES = {1: "activado", 3: "en modo «bloquear la modificación de discos»"}


def _norm_win_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path).strip().strip('"')))


@dataclass
class CfaStatus:
    mode: Optional[int]                    # EnableControlledFolderAccess
    allowed: List[str] = field(default_factory=list)
    exe: str = ""                          # ejecutable actual (sys.executable)

    @property
    def exe_allowed(self) -> bool:
        return bool(self.exe) and _norm_win_path(self.exe) in {_norm_win_path(a) for a in self.allowed}

    @property
    def blocks(self) -> bool:
        """¿Bloqueará la escritura directa en el disco destino?"""
        return self.mode in CFA_BLOCKING_MODES and not self.exe_allowed


def parse_cfa_preference(output, exe: str) -> Optional[CfaStatus]:
    """Interpreta ``Get-MpPreference | Select-Object EnableControlledFolderAccess,
    ControlledFolderAccessAllowedApplications | ConvertTo-Json`` (texto o dict).
    ``None`` si no se puede saber (p. ej. Defender no es el antivirus activo)."""
    data = output
    if isinstance(output, str):
        text = output.lstrip("﻿").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    mode = data.get("EnableControlledFolderAccess")
    try:
        mode = int(mode) if mode is not None else None
    except (TypeError, ValueError):
        mode = None
    allowed = data.get("ControlledFolderAccessAllowedApplications") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    return CfaStatus(mode, [str(a) for a in allowed if a], exe or "")


def cfa_banner_text(status: CfaStatus) -> str:
    """Aviso del Resumen cuando el Acceso controlado a carpetas bloqueará la clonación."""
    how = CFA_BLOCKING_MODES.get(status.mode, "activado")
    return (f"El «Acceso controlado a carpetas» de Windows Defender está {how} y "
            f"{status.exe} no está entre sus aplicaciones permitidas. Windows bloqueará la "
            "escritura en el disco destino (y bcdboot avisará «BFSVC Warning: Failed to flush "
            "system partition. Error = [5]»). Permite ClonDisk antes de clonar.")


def bcdboot_warnings(output: str) -> List[str]:
    """Explica avisos conocidos de bcdboot que no detienen la copia de archivos."""
    notes = []
    if re.search(r"BFSVC.*Error\s*=\s*\[5\]", output or "", re.IGNORECASE):
        notes.append("bcdboot no pudo vaciar la caché de la partición de sistema (error 5, acceso "
                     "denegado). Suele deberse al «Acceso controlado a carpetas» de Windows "
                     "Defender; los archivos de arranque sí se copian (la comprobación del "
                     "arranque lo confirma). Para evitarlo, permite ClonDisk en esa protección.")
    return notes


# ---------------------------------------------------------------------------
# Escritura denegada en el destino
# ---------------------------------------------------------------------------
def write_denied_advice(controlled_folder_access: Optional[int], exe: str) -> str:
    """Diagnóstico de un «Acceso denegado (5)» al escribir en el disco destino.
    ``controlled_folder_access``: valor de Get-MpPreference EnableControlledFolderAccess
    (0 = desactivado, 1 = activado, 2 = solo auditoría, 3 = bloquear la modificación de
    discos, None = desconocido)."""
    app = exe or "ClonDisk.exe / python.exe"
    if controlled_folder_access in CFA_BLOCKING_MODES:
        return ("El «Acceso controlado a carpetas» de Windows Defender está ACTIVADO y bloquea "
                "la escritura directa en discos (sectores de arranque) de las aplicaciones que "
                "no reconoce.\n\nPara permitir ClonDisk: Seguridad de Windows → Protección "
                "contra virus y amenazas → Administrar la protección contra ransomware → "
                "Permitir una aplicación mediante Acceso controlado a carpetas → Agregar una "
                f"aplicación permitida → {app}\n\nO, en PowerShell como administrador:\n"
                f"Add-MpPreference -ControlledFolderAccessAllowedApplications \"{app}\"\n\n"
                "Después vuelve a iniciar la clonación (no hace falta reiniciar).")
    return ("Windows denegó la escritura en el disco destino. Comprueba que ClonDisk se ejecuta "
            "como administrador y que ningún programa está usando el disco. Si tu antivirus tiene "
            "protección del MBR o de sectores de arranque (o la BIOS tiene «Boot sector virus "
            "protection»), desactívala temporalmente o agrega una excepción para "
            f"{app}.")


# ---------------------------------------------------------------------------
# bcdedit
# ---------------------------------------------------------------------------
def bcdedit_partition_letters(output: str) -> Set[str]:
    """Letras que aparecen como ``partition=X:`` (device/osdevice) en ``bcdedit /enum``.
    No depende del idioma: los nombres de campo se traducen, el valor no."""
    return {m.upper() for m in re.findall(r"partition=([A-Za-z]):", output)}
