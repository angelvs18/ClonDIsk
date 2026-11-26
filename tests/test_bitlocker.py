"""BitLocker en el disco de origen: estado por WMI, avisos, textos y flujo del asistente."""
import json
import os
import time

import pytest

from clondisk.core.bitlocker import (REENABLE_HINT, BitLockerVolume, assess_bitlocker,
                                     decrypt_confirm_text, decrypt_error_text,
                                     estimate_seconds_left, fmt_eta, parse_bitlocker_volumes)
from clondisk.core.model import GiB, Disk, Partition
from clondisk.core.planner import CloneOptions, build_plan
from clondisk.core.winprep import post_migration_checklist

GUID_BASIC = "EBD0A0A2-B9E5-4433-87C0-68B9B6D7C1C7"
C_OFFSET = 1318060032                       # offset real de C: (Get-Partition)


def bl_disk(fs="BitLocker", media="HDD"):
    return Disk(0, r"\\.\PhysicalDrive0", "ST1000DM010", 1000 * GiB, style="GPT", media=media,
                partitions=[Partition(4, C_OFFSET // 512, 900 * GiB // 512, type_guid=GUID_BASIC,
                                      fs=fs, letter="C", used_bytes=None if fs == "BitLocker"
                                      else 40 * GiB)])


def vol(conv=1, pct=100.0, prot=1, lock=0, rp=1, offset=C_OFFSET):
    return BitLockerVolume(offset, "\\\\?\\Volume{1b2c}\\", "C", conv, pct, prot,
                           lock == 1, rp)


# ---------------------------------------------------------------- parseo
def test_parse_list_single_object_bom_and_letters():
    rows = [{"Offset": C_OFFSET, "DeviceID": r"\\?\Volume{abc}\\", "DriveLetter": "C:",
             "Conversion": 3, "Percent": 42.5, "Protection": 0, "Lock": 0, "RecoveryPasswords": 1},
            {"DeviceID": "sin offset"}]
    vols = parse_bitlocker_volumes("\ufeff" + json.dumps(rows))
    assert len(vols) == 1
    v = vols[0]
    assert (v.offset, v.letter, v.conversion, v.percent, v.protection, v.locked,
            v.recovery_passwords) == (C_OFFSET, "C", 3, 42.5, 0, False, 1)
    assert v.decrypting and not v.decrypted and v.can_decrypt is False
    # ConvertTo-Json sin -InputObject @() devuelve un objeto suelto
    one = parse_bitlocker_volumes(json.dumps(rows[0]))
    assert len(one) == 1 and one[0].letter == "C"
    assert parse_bitlocker_volumes({"Offset": "1048576", "DriveLetter": "", "Lock": 1})[0].locked
    assert parse_bitlocker_volumes({"Offset": 5, "DriveLetter": None})[0].letter is None
    assert parse_bitlocker_volumes("") == [] and parse_bitlocker_volumes("[]") == []


def test_state_texts():
    assert vol(1).state_text() == "cifrado"
    assert vol(1, prot=0).state_text() == "cifrado, protección suspendida"
    assert vol(3, 42.5).state_text() == "descifrándose (queda 42.5 % cifrado)"
    assert vol(1, lock=1, prot=2).state_text() == "bloqueado"
    assert vol(0, 0).state_text() == "descifrado"
    for c in (1, 2, 4, 5):
        assert vol(c).can_decrypt
    assert not vol(1, lock=1).can_decrypt and not vol(0).can_decrypt and not vol(3).can_decrypt


# ---------------------------------------------------------------- assess
def test_assess_none_without_bitlocker():
    assert assess_bitlocker(bl_disk("NTFS"), []) is None


def test_assess_encrypted_and_suspended():
    for prot, text in ((1, "C: — cifrado."), (0, "C: — cifrado, protección suspendida.")):
        rep = assess_bitlocker(bl_disk(), [vol(1, prot=prot)])
        assert rep.level == "error" and text in rep.lines
        assert len(rep.decryptable) == 1 and not rep.decrypting and not rep.all_decrypted
        assert "suspender BitLocker no basta" in rep.lines[0]
        assert not any("C::" in x for x in rep.lines)


def test_assess_decrypting_finished_locked_and_unknown():
    rep = assess_bitlocker(bl_disk(), [vol(3, 42.5)])
    assert rep.level == "info" and rep.decrypting and not rep.decryptable
    assert "C: — descifrándose (queda 42.5 % cifrado)." in rep.lines

    rep = assess_bitlocker(bl_disk(), [vol(0, 0.0)])
    assert rep.level == "ok" and rep.all_decrypted

    rep = assess_bitlocker(bl_disk(), [vol(1, lock=1, prot=2)])
    assert rep.level == "error" and not rep.decryptable
    assert any("Explorador" in x and "Actualizar estado" in x for x in rep.lines)

    for vols in (None, [], [vol(offset=123)]):
        rep = assess_bitlocker(bl_disk(), vols)
        assert rep.level == "error" and not rep.decryptable
        joined = " ".join(rep.lines)
        assert "manage-bde -off C:" in joined and "manage-bde -status C:" in joined


# ---------------------------------------------------------------- textos
def test_confirm_text():
    d = bl_disk()
    t = decrypt_confirm_text(d, [(d.partitions[0], vol())])
    for needle in ("ORIGEN", "No se pierden datos", "seguir usando el equipo", "reinicias",
                   "varias horas", "SIN cifrar", "clave de recuperación", "manage-bde -protectors"):
        assert needle in t, needle
    assert "C::" not in t
    ssd = bl_disk(media="SSD")
    t = decrypt_confirm_text(ssd, [(ssd.partitions[0], vol(rp=0))])
    assert "varias horas" not in t and "clave de recuperación" not in t


def test_error_codes():
    assert "bloqueado" in decrypt_error_text(0x80310000) and "0x80310000" in decrypt_error_text(0x80310000)
    assert "no está cifrado" in decrypt_error_text(0x80310001)
    t = decrypt_error_text(0x80310029, "D:")
    assert "manage-bde -autounlock -clearallkeys D:" in t and "0x80310029" in t
    assert "0x80070005" in decrypt_error_text(0x80070005)
    assert "0xFFFFFFFF" in decrypt_error_text(-1)


def test_eta():
    assert estimate_seconds_left([]) is None and estimate_seconds_left([(0, 50)]) is None
    assert estimate_seconds_left([(0, 50), (60, 50)]) is None           # sin avance
    assert estimate_seconds_left([(0, 50), (100, 49), (600, 40)]) == pytest.approx(2400)
    assert fmt_eta(None) == "calculando…"
    assert fmt_eta(30) == "menos de un minuto"
    assert fmt_eta(45 * 60) == "~45 min restantes"
    assert fmt_eta(2 * 3600 + 5 * 60) == "~2 h 05 min restantes"


# ---------------------------------------------------------------- planner y lista final
def test_planner_message_and_checklist():
    tgt = Disk(1, r"\\.\PhysicalDrive1", "SSD", 2000 * GiB, style="GPT")
    plan = build_plan(bl_disk(), tgt, CloneOptions())
    msg = " ".join(plan.errors)
    assert "«Descifrar…»" in msg and "suspender BitLocker no basta" in msg
    assert "manage-bde -off C:" in msg
    base, with_bl = post_migration_checklist(True), post_migration_checklist(True, bitlocker=True)
    assert REENABLE_HINT not in base and REENABLE_HINT in with_bl
    assert len(with_bl) == len(base) + 1 and with_bl[-1].startswith("Guarda el disco de origen")
    assert "manage-bde -on C: -UsedSpaceOnly" in REENABLE_HINT


# ---------------------------------------------------------------- asistente
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 no está disponible")
    from clondisk.gui import theme
    inst = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    theme.apply(inst)
    return inst


def _wait(app, w, timeout=10.0):
    end = time.monotonic() + timeout
    while (w._busy or w._bl_running) and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not (w._busy or w._bl_running)


def _fake_platform():
    from clondisk.platforms.base import Platform

    class FakeBitLocker(Platform):
        supports_bitlocker = True

        def __init__(self):
            self.state = "encrypted"
            self.decrypt_calls = []
            self.ntfs_after = True
            self.tgt = Disk(1, r"\\.\PhysicalDrive1", "SSD", 2000 * GiB, style="GPT", media="SSD")

        def bitlocker_status(self, disk):
            return [{"encrypted": vol(1), "decrypting": vol(3, 42.5),
                     "done": vol(0, 0.0)}[self.state]]

        def decrypt_bitlocker(self, v, log):
            self.decrypt_calls.append(v.device_id)
            self.state = "decrypting"

        def list_disks(self):
            return [bl_disk("NTFS" if self.ntfs_after else "BitLocker"), self.tgt]

    return FakeBitLocker()


def test_wizard_decrypt_flow(app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from clondisk.gui.wizard import CloneWizard
    plat = _fake_platform()
    info = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: info.append(a[2]))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: pytest.fail(a[2]))
    src = bl_disk()
    w = CloneWizard(plat, [src, plat.tgt], src)          # preseleccionado («Clonar este disco…»)
    try:
        _wait(app, w)
        assert not w.bl_box.isHidden() and not w.btn_next.isEnabled()
        assert not w.btn_bl_decrypt.isHidden()
        assert "C: — cifrado." in w.bl_label.text()

        w._bl_decrypt()
        _wait(app, w)
        assert plat.decrypt_calls and w.bl_timer.isActive()
        assert "descifrándose" in w.bl_label.text() and "calculando" in w.bl_label.text()
        assert w.btn_bl_decrypt.isHidden() and not w.btn_next.isEnabled()

        plat.state, plat.ntfs_after = "done", False       # sector de arranque todavía -FVE-FS-
        w._bl_check(force=True)
        _wait(app, w)
        assert w.bl_timer.isActive() and not w.btn_next.isEnabled() and not info

        plat.ntfs_after = True
        w._bl_check(force=True)
        _wait(app, w)
        assert not w.bl_timer.isActive() and w.bl_box.isHidden()
        assert w.source.partitions[0].fs == "NTFS" and w.btn_next.isEnabled()
        assert info and "ya puedes continuar" in info[0].lower()
        assert w.options.bitlocker_decrypted
        w._read_options()
        assert w.options.bitlocker_decrypted
    finally:
        w.close()


def test_wizard_base_platform_manual_steps(app):
    from clondisk.gui.wizard import CloneWizard
    from clondisk.platforms.base import Platform
    src = bl_disk()
    w = CloneWizard(Platform(), [src], None)
    try:
        w.show_page(0)
        w._pick_source(src)
        _wait(app, w)
        text = w.bl_label.text()
        assert not w.bl_box.isHidden() and "manage-bde -off C:" in text
        assert w.btn_bl_decrypt.isHidden() and not w.btn_next.isEnabled()
        assert not w.bl_timer.isActive()
    finally:
        w.close()


# ---------------------------------------------------------------- correcciones de la revisión
VOL_ABCD = r"\\?\Volume{abcd}" + "\\"


def _no_letter_disk():
    d = bl_disk()
    d.partitions[0].letter = None
    return d


def test_partition_without_letter_never_says_c():
    d = _no_letter_disk()
    part = d.partitions[0]
    with_vol = BitLockerVolume(C_OFFSET, VOL_ABCD, None, 1, 100.0, 1, False, 1)
    texts = []
    for vols in (None, [], [with_vol]):
        texts += assess_bitlocker(d, vols).lines
    texts.append(decrypt_confirm_text(d, [(part, with_vol)]))
    texts.append(decrypt_error_text(0x80310029))
    for t in texts:
        assert "C:" not in t, t
    joined = " ".join(assess_bitlocker(d, None).lines)
    assert "manage-bde -off <unidad>:" in joined
    assert VOL_ABCD in decrypt_confirm_text(d, [(part, with_vol)])
    locked = BitLockerVolume(C_OFFSET, VOL_ABCD, None, 7, None, 2, False, 0)
    assert "manage-bde -off " + VOL_ABCD in " ".join(assess_bitlocker(d, [locked]).lines)
    assert "<unidad>:" in decrypt_error_text(0x80310029)


def test_wizard_shows_query_error_detail(app):
    from clondisk.gui.wizard import CloneWizard
    from clondisk.platforms.base import Platform

    class Broken(Platform):
        supports_bitlocker = True

        def bitlocker_status(self, disk):
            raise RuntimeError(r"Invalid namespace <root\cimv2\Security>")

    src = bl_disk()
    w = CloneWizard(Broken(), [src], src)
    try:
        _wait(app, w)
        text = w.bl_label.text()
        assert "Invalid namespace" in text and "manage-bde -off" in text
        assert "Detalle:" in text and "&lt;root" in text          # escapado
        assert not w.btn_next.isEnabled() and w.btn_bl_decrypt.isHidden()
    finally:
        w.close()
