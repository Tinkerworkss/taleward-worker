"""Fehler bleiben sichtbar und Prozessbäume sterben ganz – Dinge, die erst auf fremden Rechnern auffallen."""
import os
import subprocess
import sys
import time

import httpx
import pytest

from taleward_worker import prozesse, server
from taleward_worker.dienst import Dienst
from taleward_worker.einstellungen import Einstellungen


def _warten(bedingung, sekunden=5.0):
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.05)
    return bedingung()


def _lebt(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # Zombie zählt nicht
        return "Z" not in open(f"/proc/{pid}/stat").read().split(")")[1].split()[0]
    except OSError:
        return True


@pytest.mark.skipif(sys.platform == "win32", reason="Prozessgruppe – unter Windows übernimmt das Job-Objekt")
def test_beenden_erwischt_auch_enkel(tmp_path):
    kennung = tmp_path / "enkel.pid"
    p = subprocess.Popen([sys.executable, "-c", f"""
import subprocess, sys, time, pathlib
k = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(r'{kennung}').write_text(str(k.pid))
time.sleep(60)
"""], **prozesse.start_argumente())
    assert _warten(lambda: kennung.exists() and kennung.read_text().strip())
    enkel = int(kennung.read_text())
    assert _lebt(enkel)
    prozesse.beenden(p, warten=1)
    assert p.poll() is not None
    assert _warten(lambda: not _lebt(enkel), 3), "Enkel läuft weiter"


def test_beenden_vertraegt_nichts_und_tote_prozesse():
    prozesse.beenden(None)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    prozesse.beenden(p)  # schon tot – kein Fehler
    assert prozesse.zuordnen(p) is False or sys.platform == "win32"


def test_html_statt_json_ist_kein_taleward(monkeypatch):
    """Hotel-Anmeldeseite oder Proxy-Sperrseite antwortet mit 200 und HTML: ein sauberer ServerFehler statt einer
    Ausnahme, die den Hintergrund-Thread der App still sterben lässt."""
    def antwort(req: httpx.Request):
        return httpx.Response(200, text="<html><body>Bitte anmelden</body></html>")

    monkeypatch.setattr(server, "_klient", lambda: httpx.Client(transport=httpx.MockTransport(antwort)))
    with pytest.raises(server.ServerFehler) as e:
        server.konfiguration("https://x", "t")
    assert e.value.code == "kein_taleward"

    def liste(req: httpx.Request):
        return httpx.Response(201, json=[1, 2])

    monkeypatch.setattr(server, "_klient", lambda: httpx.Client(transport=httpx.MockTransport(liste)))
    with pytest.raises(server.ServerFehler):
        server.koppeln("https://x", "ABCD1234", "w")


def test_waechter_ueberlebt_fehler(monkeypatch):
    d = Dienst(Einstellungen(server="x", token="t"), befehl=[sys.executable, "-c", "pass"])
    zaehler = {"n": 0}

    def kaputt():
        zaehler["n"] += 1
        raise RuntimeError("peng")

    monkeypatch.setattr(d, "_motor_starten", kaputt)
    with d._sperre:
        d._soll_laufen, d._neustart_um = True, time.monotonic() - 1
    assert _warten(lambda: zaehler["n"] >= 1, 4)
    assert any("Fehler im Wächter" in z and "peng" in z for z in d.protokoll.zeilen)
    with d._sperre:
        d._neustart_um = time.monotonic() - 1
    assert _warten(lambda: zaehler["n"] >= 2, 4), "Wächter ist gestorben"
    d.stoppen()


def test_absturz_wird_protokolliert(eigener_ordner, monkeypatch):
    from taleward_worker import __main__ as haupt, pfade

    monkeypatch.setattr(sys, "platform", "linux")
    haupt.absturz_melden("Traceback …\nRuntimeError: peng")
    assert "peng" in (pfade.protokolle() / "absturz.log").read_text(encoding="utf-8")


def test_einstellungen_ueberleben_kaputte_datei(eigener_ordner):
    from taleward_worker import pfade

    e = Einstellungen(server="https://x", token="geheim")
    e.speichern()
    e.name = "zweiter"
    e.speichern()
    assert pfade.einstellungen().with_suffix(".bak").exists()
    pfade.einstellungen().write_text("")  # Stromausfall: leere Datei
    geladen = Einstellungen.laden()
    assert geladen.token == "geheim" and geladen.server == "https://x"
    assert not pfade.einstellungen().with_suffix(".tmp").exists()


def test_einstellungen_speichern_mit_geduld(eigener_ordner, monkeypatch):
    from taleward_worker import einstellungen as modul

    versuche = {"n": 0}
    echt = os.replace

    def zickig(a, b):
        versuche["n"] += 1
        if versuche["n"] < 3:
            raise PermissionError("[WinError 32] in Benutzung")
        echt(a, b)

    monkeypatch.setattr(modul.os, "replace", zickig)
    monkeypatch.setattr(modul.time, "sleep", lambda s: None)
    Einstellungen(server="https://x", token="t").speichern()
    assert versuche["n"] == 3 and Einstellungen.laden().token == "t"


def test_fehlercodes_aus_uv_ausgabe():
    from taleward_worker.motor import fehlercode

    assert fehlercode("There is not enough space on the disk. (os error 112)") == "kein_platz"
    assert fehlercode("No space left on device") == "kein_platz"
    assert fehlercode("The filename or extension is too long. (os error 206)") == "pfad_zu_lang"
    assert fehlercode("Access is denied. (os error 5)") == "dateien_in_benutzung"
    assert fehlercode("error sending request: dns error") == "netz"
    assert fehlercode("invalid peer certificate: UnknownIssuer") == "netz"
    assert fehlercode("irgendwas") == "installation"


def test_umbenennen_mit_geduld(tmp_path, monkeypatch):
    from taleward_worker import motor

    monkeypatch.setattr(motor.time, "sleep", lambda s: None)
    von, nach = tmp_path / "neu", tmp_path / "alt"
    von.mkdir()
    echt = motor.Path.rename
    versuche = {"n": 0}

    def zickig(self, ziel):
        versuche["n"] += 1
        if versuche["n"] < 4:
            raise PermissionError("[WinError 5] Zugriff verweigert")
        return echt(self, ziel)

    monkeypatch.setattr(motor.Path, "rename", zickig)
    motor.umbenennen(von, nach)
    assert nach.exists() and not von.exists() and versuche["n"] == 4

    monkeypatch.setattr(motor.Path, "rename", lambda self, ziel: (_ for _ in ()).throw(PermissionError("x")))
    with pytest.raises(motor.MotorFehler) as e:
        motor.umbenennen(nach, von, versuche=2)
    assert e.value.code == "dateien_in_benutzung"


def test_gescheiterter_tausch_wird_nicht_als_neue_fassung_gezaehlt(eigener_ordner, monkeypatch):
    """Scheitert der Tausch, bleibt der alte Motor und die App merkt sich NICHT die neue Fassung."""
    import json
    from pathlib import Path

    from taleward_worker import motor, pfade

    monkeypatch.setattr(motor, "quelle", lambda f: {"ref": f"v{f}", "genau": True, "paket": "p.zip",
                                                    "anforderungen": "egal"})
    monkeypatch.setattr(motor, "anforderungen_lesen", lambda q: "torch==2.8.0\n")
    monkeypatch.setattr(motor, "installiert", lambda: None)
    monkeypatch.setattr(motor, "ffmpeg_einrichten", lambda py: None)
    monkeypatch.setattr(motor, "home_festlegen", lambda venv, py: None)
    monkeypatch.setattr(motor, "torch_pruefen", lambda py: {"cuda": "12.8"})
    monkeypatch.setattr(motor.Installation, "_python_einrichten", lambda self: Path("python"))
    monkeypatch.setattr(motor.Installation, "_uv", lambda self, *a, **k: Path(a[1]).mkdir(parents=True, exist_ok=True) if a[0] == "venv" else None)
    monkeypatch.setattr(motor, "_python_pruefen", lambda *a, **k: subprocess.CompletedProcess(a, 0, "ok\n", ""))
    monkeypatch.setattr(motor, "umbenennen", lambda v, n, **k: (_ for _ in ()).throw(motor.MotorFehler("dateien_in_benutzung", "x")))
    pfade.motor().mkdir(parents=True)
    (pfade.motor() / "taleward-motor.json").write_text(json.dumps({"fassung": "alt"}))
    phasen = []
    inst = motor.Installation("0.4.19", nach_tausch=lambda: phasen.append(inst.phase))
    inst._lauf()
    assert phasen == ["fehler"] and inst.fehler == "dateien_in_benutzung"
    assert json.loads((pfade.motor() / "taleward-motor.json").read_text())["fassung"] == "alt"


def test_zertifikatsfehler_wird_erkannt(monkeypatch):
    import ssl

    innen = ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate")
    aussen = httpx.ConnectError("x")
    aussen.__cause__ = innen
    assert server.zertifikatsfehler(aussen)
    assert not server.zertifikatsfehler(httpx.ConnectError("Connection refused"))

    def antwort(req: httpx.Request):
        raise aussen

    monkeypatch.setattr(server, "_klient", lambda: httpx.Client(transport=httpx.MockTransport(antwort)))
    with pytest.raises(server.ServerFehler) as e:
        server.pruefen("https://x")
    assert e.value.code == "zertifikat"
    with pytest.raises(server.ServerFehler) as e:
        server.konfiguration("https://x", "t")
    assert e.value.code == "zertifikat"


def test_fenster_passt_auf_kleine_bildschirme():
    from taleward_worker.oberflaeche import fenstergroesse, webview2_vorhanden

    assert fenstergroesse(None) == (560, 760, (460, 600))
    assert fenstergroesse(1080) == (560, 760, (460, 600))
    b, h, mind = fenstergroesse(614)  # 1366×768 bei 125 %
    assert h == 534 and mind == (460, 534)
    assert fenstergroesse(512)[1] == 432  # 150 %
    assert webview2_vorhanden() or sys.platform == "win32"


def test_kleine_bibliotheksaenderung_geht_in_den_vorhandenen_motor(eigener_ordner, monkeypatch):
    """Kommt nur ein kleines Paket dazu (truststore), wird es eingespielt – kein neues 8-GB-Paket."""
    import json
    import subprocess as sp

    from taleward_worker import motor, pfade

    assert motor.geaenderte_pakete(" + truststore==0.10.4\n - alt==1\nWould install 1 package") == ["truststore", "alt"]
    assert motor.schwer("torch==2.8.0") and motor.schwer("nvidia-cudnn-cu12") and not motor.schwer("truststore")

    py = pfade.motor_python()
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")
    (pfade.motor() / "taleward-motor.json").write_text(json.dumps({"fassung": "0.4.19", "backend": "cuda"}))
    monkeypatch.setattr(motor, "quelle", lambda f: {"ref": f"v{f}", "genau": True, "paket": "paket.zip",
                                                    "anforderungen": "egal"})
    monkeypatch.setattr(motor, "anforderungen_lesen", lambda q: "torch==2.8.0\ntruststore==0.10.4\n")
    monkeypatch.setattr(motor, "uv_programm", lambda: "uv")

    def run(befehl, **kw):
        if "--dry-run" in befehl:
            return sp.CompletedProcess(befehl, 0, "Would install 1 package\n + truststore==0.10.4\n", "")
        return sp.CompletedProcess(befehl, 0, "ok\n", "")

    monkeypatch.setattr(motor.subprocess, "run", run)
    uv = []
    monkeypatch.setattr(motor.Installation, "_uv", lambda self, *a, **k: uv.append(a))
    inst = motor.Installation("0.4.20")
    inst._lauf()
    assert inst.phase == "fertig" and motor.installiert()["fassung"] == "0.4.20"
    assert len(uv) == 2 and "-r" in uv[0] and "--torch-backend" in uv[0] and "--reinstall" in uv[1]

    # torch dabei → vollständige Installation
    monkeypatch.setattr(motor.subprocess, "run",
                        lambda b, **k: sp.CompletedProcess(b, 0, "Would install 2 packages\n + torch==2.9.0\n + x==1\n", ""))
    assert motor.Installation("0.4.21")._aenderung(motor.quelle("0.4.21"))[0] == "gross"


def test_ollama_nimmt_vorhandene_eigene_instanz(monkeypatch):
    from taleward_worker import ollama

    d = ollama.OllamaDienst()
    monkeypatch.setattr(ollama, "antwortet", lambda adresse, *a: adresse == d.eigene_adresse)
    monkeypatch.setattr(ollama, "installiert", lambda: True)
    monkeypatch.setattr(d, "starten", lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein zweiter Start")))
    assert d.adresse(True) == d.eigene_adresse
    # nichts antwortet, Programm fehlt → AUS mit Grund
    monkeypatch.setattr(ollama, "antwortet", lambda adresse, *a: False)
    monkeypatch.setattr(ollama, "programm", lambda: None)
    monkeypatch.setattr(d, "starten", ollama.OllamaDienst.starten.__get__(d))
    assert d.adresse(True) == ollama.AUS and d.fehler == "programm_fehlt"


def test_recaps_nur_mit_genug_hardware():
    from taleward_worker import hardware

    assert hardware.recaps_moeglich(8192, 16)
    assert hardware.recaps_moeglich(None, 32)
    assert not hardware.recaps_moeglich(4096, 8)
    assert not hardware.recaps_moeglich(None, 8)
    assert hardware.recaps_moeglich(None, None)  # unbekannt: nicht sperren


def test_platzpruefung_vor_der_installation(monkeypatch):
    from taleward_worker import motor

    monkeypatch.setattr(motor.shutil, "disk_usage", lambda p: type("U", (), {"free": 5 * 1024 ** 3})())
    assert motor.platz_fehlt_gb(False, "cuda") > 10
    assert motor.platz_fehlt_gb(True, "cuda") == 0
    monkeypatch.setattr(motor.shutil, "disk_usage", lambda p: type("U", (), {"free": 40 * 1024 ** 3})())
    assert motor.platz_fehlt_gb(False, "cuda") == 0


def test_installer_kann_die_app_hoeflich_beenden(eigener_ordner):
    from taleward_worker import einzeln

    gezeigt, beendet = [], []
    srv = einzeln.lauschen(lambda: gezeigt.append(1), lambda: beendet.append(1))
    try:
        assert einzeln.andere_wecken() and gezeigt == [1]
        assert einzeln._senden(einzeln.ABSCHIED)
        assert _warten(lambda: beendet == [1])
        assert einzeln.sperren()  # außerhalb von Windows immer frei
    finally:
        import socket

        srv.shutdown(socket.SHUT_RDWR)  # weckt den wartenden accept – sonst lebt der Port im Test weiter
        srv.close()
    assert _warten(lambda: not einzeln.andere_wecken())
    assert einzeln.andere_beenden(warten_s=0.5)  # nichts läuft → gilt als beendet


def test_beenden_schalter(eigener_ordner, monkeypatch):
    from taleward_worker import __main__ as haupt, einzeln

    monkeypatch.setattr(sys, "argv", ["taleward-worker", "--beenden"])
    monkeypatch.setattr(einzeln, "andere_beenden", lambda: True)
    assert haupt.main() == 0
    monkeypatch.setattr(einzeln, "andere_beenden", lambda: False)
    assert haupt.main() == 1
