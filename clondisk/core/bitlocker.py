"""BitLocker en el disco de origen: estado, avisos y textos del descifrado.

Suspender BitLocker no sirve para clonar: los datos siguen cifrados en el disco y la copia
inteligente lee el sector de arranque y ``$Bitmap`` en crudo. Hay que descifrar por completo.

El estado se obtiene por WMI (``Win32_EncryptableVolume``), no de la salida de
``manage-bde``, que depende del idioma. Aquí solo está la lógica pura: interpretar esa
salida, emparejarla con las particiones y redactar los mensajes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .model import Disk, Partition

# Win32_EncryptableVolume.GetConversionStatus → ConversionStatus
FULLY_DECRYPTED, FULLY_ENCRYPTED, ENCRYPTING, DECRYPTING, ENCRYPTION_PAUSED, DECRYPTION_PAUSED = range(6)
DECRYPTABLE = (FULLY_ENCRYPTED, ENCRYPTING, ENCRYPTION_PAUSED, DECRYPTION_PAUSED)

POLL_SECONDS = 20


@dataclass
class BitLockerVolume:
    offset: int                         # bytes desde el inicio del disco
    device_id: str = ""                 # \\?\Volume{...}\
    letter: Optional[str] = None        # «C» (sin dos puntos)
    conversion: Optional[int] = None    # ConversionStatus 0–5
    percent: Optional[float] = None     # EncryptionPercentage (lo que queda cifrado)
    protection: Optional[int] = None    # ProtectionStatus: 0 off, 1 on, 2 desconocido
    locked: bool = False
    recovery_passwords: int = 0         # protectores «contraseña numérica» (tipo 3)

    @property
    def decrypted(self) -> bool:
        return self.conversion == FULLY_DECRYPTED

    @property
    def decrypting(self) -> bool:
        return self.conversion == DECRYPTING

    @property
    def can_decrypt(self) -> bool:
        return not self.locked and self.conversion in DECRYPTABLE

    def _pct(self) -> str:
        return f"{self.percent:g} %" if self.percent is not None else "? %"

    def state_text(self) -> str:
        if self.locked:
            return "bloqueado"
        c = self.conversion
        if c == FULLY_DECRYPTED:
            return "descifrado"
        if c == DECRYPTING:
            return f"descifrándose (queda {self._pct()} cifrado)"
        if c == DECRYPTION_PAUSED:
            return f"descifrado en pausa (queda {self._pct()} cifrado)"
        if c == ENCRYPTING:
            return f"cifrándose ({self._pct()} cifrado)"
        if c == ENCRYPTION_PAUSED:
            return f"cifrado en pausa ({self._pct()} cifrado)"
        if c == FULLY_ENCRYPTED:
            return "cifrado, protección suspendida" if self.protection == 0 else "cifrado"
        return "estado desconocido"


def _int(value) -> Optional[int]:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def parse_bitlocker_volumes(data) -> List[BitLockerVolume]:
    """Interpreta el JSON de :meth:`WindowsPlatform.bitlocker_status` (texto, objeto suelto
    o lista). Las filas sin ``Offset`` se ignoran."""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        text = data.lstrip("\ufeff").strip()
        if not text:
            return []
        data = json.loads(text)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    out: List[BitLockerVolume] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        off = _int(row.get("Offset"))
        if off is None:
            continue
        letter = (row.get("DriveLetter") or "").strip().rstrip(":").rstrip("\\").strip() or None
        pct = row.get("Percent")
        try:
            pct = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
        out.append(BitLockerVolume(
            offset=off, device_id=str(row.get("DeviceID") or ""),
            letter=letter.upper() if letter else None,
            conversion=_int(row.get("Conversion")), percent=pct,
            protection=_int(row.get("Protection")),
            locked=_int(row.get("Lock")) == 1,
            recovery_passwords=_int(row.get("RecoveryPasswords")) or 0))
    return out


Pair = Tuple[Partition, BitLockerVolume]


@dataclass
class BitLockerReport:
    level: str                                  # "error" | "info" | "ok"
    lines: List[str] = field(default_factory=list)
    decryptable: List[Pair] = field(default_factory=list)
    decrypting: List[Pair] = field(default_factory=list)
    all_decrypted: bool = False


def bitlocker_partitions(disk: Optional[Disk]) -> List[Partition]:
    return [p for p in disk.partitions if p.fs == "BitLocker"] if disk else []


UNKNOWN_DRIVE = "<unidad>:"


def volume_arg(letter: Optional[str] = None, device_id: str = "") -> str:
    """Cómo nombrar el volumen en ``manage-bde``: la letra («D:»), si no la ruta
    ``\\\\?\\Volume{...}\\`` (manage-bde la acepta) y, si no hay nada, «<unidad>:».
    Nunca se supone C:."""
    if letter:
        return f"{letter.rstrip(':')}:"
    return device_id or UNKNOWN_DRIVE


def _drive(part: Partition, vol: Optional[BitLockerVolume] = None) -> str:
    return volume_arg(part.letter or (vol.letter if vol else None), vol.device_id if vol else "")


def _manual_steps(drive: str) -> str:
    return (f"Para descifrarla a mano, ejecuta como administrador «manage-bde -off {drive}» y "
            f"revisa el avance con «manage-bde -status {drive}» (debe llegar a «Totalmente "
            "descifrado» / «Fully Decrypted»).")


def assess_bitlocker(disk: Disk, vols: Optional[Sequence[BitLockerVolume]]) -> Optional[BitLockerReport]:
    """Estado de las particiones BitLocker de ``disk`` (``None`` si no tiene ninguna).
    ``vols=None`` significa que no se pudo consultar (o la plataforma no lo admite)."""
    parts = bitlocker_partitions(disk)
    if not parts:
        return None
    by_off = {v.offset: v for v in (vols or [])}
    rep = BitLockerReport(level="error")
    body: List[str] = []
    manual: List[str] = []
    locked: List[str] = []
    done = 0
    for p in parts:
        v = by_off.get(p.offset_bytes(disk.sector_size))
        name = p.display_name()
        if v is None:
            body.append(f"{name} — cifrado con BitLocker (no se pudo consultar su estado).")
            manual.append(_drive(p))
            continue
        body.append(f"{name} — {v.state_text()}.")
        if v.locked:
            locked.append(name)
        elif v.decrypted:
            done += 1
        elif v.decrypting:
            rep.decrypting.append((p, v))
        elif v.can_decrypt:
            rep.decryptable.append((p, v))
        else:
            manual.append(_drive(p, v))
    rep.all_decrypted = done == len(parts)
    if rep.all_decrypted:
        rep.level = "ok"
        lead = "Windows terminó de descifrar el disco de origen; comprobando el sector de arranque…"
    elif rep.decrypting and not (rep.decryptable or manual or locked):
        rep.level = "info"
        lead = ("Windows está descifrando el disco de origen. Puedes seguir usando el equipo "
                "(si reinicias, continúa solo); ClonDisk avisará cuando termine.")
    else:
        lead = ("El disco de origen tiene BitLocker. Hay que descifrarlo por completo antes de "
                "clonar: suspender BitLocker no basta, porque los datos siguen cifrados en el disco.")
    rep.lines = [lead] + body
    for name in locked:
        rep.lines.append(f"{name} está bloqueado: desbloquéalo en el Explorador (doble clic e "
                         "introduce la contraseña o la clave de recuperación) y pulsa «Actualizar estado».")
    for drive in manual:
        rep.lines.append(_manual_steps(drive))
    return rep


def decrypt_confirm_text(disk: Disk, pairs: Sequence[Pair]) -> str:
    names = ", ".join(p.display_name() for p, _ in pairs)
    lines = [f"Se va a DESCIFRAR {names} en el disco de ORIGEN (Disco {disk.number}, {disk.model}). "
             "Esto modifica el disco de origen.", ""]
    lines.append("• No se pierden datos: Windows descifra el volumen en segundo plano.")
    lines.append("• Puedes seguir usando el equipo mientras tanto; si reinicias, el descifrado "
                 "continúa solo.")
    if disk.media != "SSD":
        lines.append("• En un disco que no es SSD puede tardar varias horas.")
    lines.append("• Al terminar, el disco de origen queda SIN cifrar: cualquiera con acceso físico "
                 "puede leerlo. Después de clonar podrás volver a activar BitLocker en el SSD.")
    with_key = [(p, v) for p, v in pairs if v.recovery_passwords]
    for p, v in with_key:
        drive = _drive(p, v)
        lines.append(f"• {p.display_name()} tiene clave de recuperación: guárdala antes por si la "
                     f"necesitas («manage-bde -protectors -get {drive}» o tu cuenta Microsoft).")
    lines += ["", "¿Descifrar ahora?"]
    return "\n".join(lines)


DECRYPT_ERRORS = {
    0x80310000: "El volumen está bloqueado: desbloquéalo en el Explorador e inténtalo de nuevo.",
    0x80310001: "El volumen no está cifrado con BitLocker.",
    0x80310029: ("Windows no deja descifrar la unidad del sistema mientras otras unidades de "
                 "datos tienen activado el desbloqueo automático. Ejecuta como administrador "
                 "«manage-bde -autounlock -clearallkeys {drive}» e inténtalo de nuevo."),
}


def decrypt_error_text(code: int, drive: str = UNKNOWN_DRIVE) -> str:
    code &= 0xFFFFFFFF
    msg = DECRYPT_ERRORS.get(code, "Windows no pudo iniciar el descifrado.")
    return f"{msg.format(drive=drive)} (código 0x{code:08X})"


def estimate_seconds_left(samples: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Segundos hasta 0 % cifrado según la primera y la última muestra ``(t, %)``."""
    if len(samples) < 2:
        return None
    (t0, p0), (t1, p1) = samples[0], samples[-1]
    dt, dp = t1 - t0, p0 - p1
    if dt <= 0 or dp <= 0:
        return None
    return max(0.0, p1) * dt / dp


def fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "calculando…"
    if seconds < 60:
        return "menos de un minuto"
    mins = max(1, round(seconds / 60))
    if mins < 60:
        return f"~{mins} min restantes"
    return f"~{mins // 60} h {mins % 60:02d} min restantes"


REENABLE_HINT = (
    "BitLocker: el disco de origen se descifró para clonarlo. En el SSD, vuelve a activarlo "
    "(Panel de control → Cifrado de unidad BitLocker, o «manage-bde -on C: -UsedSpaceOnly» como "
    "administrador) y guarda la nueva clave de recuperación. El disco de origen quedó sin cifrar: "
    "cuando ya no lo necesites, cífralo o bórralo.")
