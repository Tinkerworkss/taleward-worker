import json
import sys
import time
from pathlib import Path

import pytest

from taleward_worker import autostart, hardware, motor, pfade, server
from taleward_worker.dienst import Dienst
from taleward_worker.einstellungen import Einstellungen


def test_einstellungen_speichern_ohne_token_in_oberflaeche():
    e = Einstellungen(server="https://t.example.org", token="wk.geheim")
    e.speichern()
    neu = Einstellungen.laden()
    assert neu.token == "wk.geheim" and neu.gekoppelt
    assert "token" not in neu.fuer_oberflaeche() and neu.fuer_oberflaeche()["gekoppelt"]
    if sys.platform != "win32":
        assert oct(pfade.einstellungen().stat().st_mode)[-3:] == "600"


def test_adressen():
    assert server.adresse_normalisieren(" taleward.example.org/ ") == "https://taleward.example.org"
    assert server.unverschluesselt("http://192.168.1.20:8000")
    assert not server.unverschluesselt("http://localhost:8000")
    assert not server.unverschluesselt("https://x.org")
    with pytest.raises(server.ServerFehler):
        server.adresse_normalisieren("  ")


def test_modellwahl_nach_grafikspeicher():
    assert hardware.modell_fuer(8192) == {"modell": "large-v3", "batch": 8}   # 8-GB-Karte
    assert hardware.modell_fuer(24576)["batch"] == 16
    assert hardware.modell_fuer(4096)["modell"] == "large-v3-turbo"


def test_profile_mit_speichergrenze():
    p = hardware.profil(8192, grenze_mb=4096)          # Schieberegler auf 4 GB
    assert (p["modell"], p["batch"], p["geraet"], p["grenzeMb"]) == ("large-v3-turbo", 4, "cuda", 4096)
    p = hardware.profil(8192, grenze_mb=2048)          # sehr knapp: Ausrichtung/Sprecher auf dem Prozessor
    assert (p["geraet"], p["ausrichten"], p["sprecher"], p["batch"]) == ("cuda", "cpu", "cpu", 2)
    assert hardware.profil(8192, grenze_mb=1024)["geraet"] == "cpu"
    assert hardware.profil(None)["stufe"] == "cpu" and hardware.profil(None)["genauigkeit"] == "int8"
    assert hardware.profil(8192, prozessor=True)["geraet"] == "cpu"
    assert hardware.profil(8192)["grenzeMb"] == 0
    # Wunsch „genau“ trotz knapper Grenze: large-v3 mit kleinerem Stapel, Rest auf dem Prozessor
    p = hardware.profil(8192, grenze_mb=4096, modell_wunsch="large-v3")
    assert (p["modell"], p["batch"], p["ausrichten"]) == ("large-v3", 2, "cpu")
    # Dauer-Schätzung wird mit weniger Speicher nicht kürzer
    assert hardware.profil(8192)["dauerMin"][1] <= hardware.profil(8192, grenze_mb=2048)["dauerMin"][1]


def test_motor_umgebung_folgt_profil(monkeypatch):
    from taleward_worker import dienst as modul

    monkeypatch.setattr(hardware, "grafikkarten", lambda frisch=False: [hardware.Grafikkarte("RTX", 8192, "576.1")])
    e = Einstellungen(server="https://x", token="t", vram_grenze_mb=4096)
    env = modul.Dienst(e)._motor_umgebung()
    assert env["WHISPER_MODEL"] == "large-v3-turbo" and env["GPU_MEMORY_LIMIT_MB"] == "4096"
    assert env["WHISPER_DEVICE"] == "cuda" and env["CPU_THREADS"] == "0"
    e.geraet = "cpu"
    env = modul.Dienst(e)._motor_umgebung()
    assert env["WHISPER_DEVICE"] == env["ALIGN_DEVICE"] == env["DIARIZE_DEVICE"] == "cpu"
    assert env["WHISPER_COMPUTE_TYPE"] == "int8" and int(env["CPU_THREADS"]) >= 1
    assert hardware.Grafikkarte("x", 8192, "576.88").treiber_ok
    assert not hardware.Grafikkarte("x", 8192, "470.10").treiber_ok


def test_quelle_lokal_und_main(monkeypatch):
    monkeypatch.setenv("TALEWARD_MOTOR_QUELLE", "/pfad/server")
    q = motor.quelle("0.4.0")
    assert q["paket"] == "/pfad/server" and q["anforderungen"].endswith("engine-requirements.txt")
    monkeypatch.delenv("TALEWARD_MOTOR_QUELLE")
    assert motor._adressen("main", False)["paket"].endswith("/refs/heads/main.zip")
    assert motor._adressen("v0.4.0", True)["paket"].endswith("/refs/tags/v0.4.0.zip")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-Autostart")
def test_autostart_linux(eigener_ordner):
    assert not autostart.aktiv()
    autostart.setzen(True)
    datei = eigener_ordner / "config" / "autostart" / "taleward-worker.desktop"
    assert autostart.aktiv() and "--hintergrund" in datei.read_text()
    autostart.setzen(False)
    assert not autostart.aktiv()


# ---------------------------------------------------------------- Dienst mit einem Schein-Motor
SCHEIN = r'''
import json, sys, time
def m(e, **d): print("@@taleward " + json.dumps({"ereignis": e, **d}), flush=True)
print("12:00:00 Worker bereit", flush=True)
m("pruefe"); m("bereit", gpu="Test-GPU", modell="large-v3"); m("warte")
m("auftrag", jobId="j1", typ="transcribe", dateien=2)
for p in (0.2, 0.6, 1.0): m("fortschritt", jobId="j1", p=p)
m("fertig", jobId="j1", sekunden=90); m("warte")
for zeile in sys.stdin:
    b = zeile.strip()
    if b == "pause": m("pausiert")
    elif b == "weiter": m("fortgesetzt")
    elif b == "server-pause": m("server_pausiert")
    elif b == "server-weiter": m("server_fortgesetzt")
    elif b == "stopp": break
m("beendet")
'''


def _warten(bedingung, sekunden=10):
    ende = time.time() + sekunden
    while time.time() < ende:
        if bedingung():
            return True
        time.sleep(0.05)
    return False


def test_dienst_verarbeitet_ereignisse(tmp_path):
    skript = tmp_path / "schein.py"
    skript.write_text(SCHEIN)
    e = Einstellungen(server="http://localhost:1", token="t")
    d = Dienst(e, befehl=[sys.executable, str(skript)])
    d.starten()
    assert _warten(lambda: d.statistik.fuer_oberflaeche()["heute"] == 1 and d.zustand["art"] == "warte")
    assert d.info["gpu"] == "Test-GPU"
    assert any("Auftrag fertig" in z for z in d.protokoll.zeilen)
    assert any(z.endswith("Worker bereit") and "12:00:00" not in z for z in d.protokoll.zeilen)
    d.pausieren(True)
    assert _warten(lambda: d.zustand["art"] == "pausiert")
    d.pausieren(False)
    assert _warten(lambda: d.zustand["art"] == "warte")
    # In der Verwaltung pausiert (der Schein-Motor spielt die Meldung des Servers nach)
    d._senden("server-pause")
    assert _warten(lambda: d.zustand["art"] == "server_pausiert")
    d.pausieren(True)  # eigene Pause geht vor
    assert _warten(lambda: d.zustand["art"] == "pausiert")
    d.pausieren(False)
    assert _warten(lambda: d.zustand["art"] == "server_pausiert")
    d._senden("server-weiter")
    assert _warten(lambda: d.zustand["art"] == "warte")
    assert any("In der Verwaltung des Servers pausiert" in z for z in d.protokoll.zeilen)
    d.stoppen()
    assert d.zustand["art"] == "gestoppt" and not d.laeuft()


def test_dienst_startet_nach_absturz_neu(tmp_path, monkeypatch):
    from taleward_worker import dienst as modul

    monkeypatch.setattr(modul, "WARTEZEITEN", [1])
    skript = tmp_path / "absturz.py"
    zaehler = tmp_path / "n"
    skript.write_text(f"import pathlib,sys; p=pathlib.Path(r'{zaehler}'); p.write_text(str(int(p.read_text() or 0)+1) "
                      "if p.exists() else '1'); sys.exit(3)")
    d = Dienst(Einstellungen(server="x", token="t"), befehl=[sys.executable, str(skript)])
    d.starten()
    assert _warten(lambda: zaehler.exists() and int(zaehler.read_text()) >= 2, 8)
    d.stoppen()


def test_dienst_ohne_motor():
    d = Dienst(Einstellungen(server="x", token="t"))
    d.starten()
    assert d.zustand == {**d.zustand, "art": "fehler", "code": "motor_fehlt"}


def test_installiert_liest_kennzeichnung():
    assert motor.installiert() is None
    py = pfade.motor_python()
    py.parent.mkdir(parents=True)
    py.write_text("")
    (pfade.motor() / "taleward-motor.json").write_text(json.dumps({"fassung": "0.4.0", "testmodus": False}))
    assert motor.installiert()["fassung"] == "0.4.0"
    motor.entfernen()
    assert not Path(pfade.motor()).exists()


# ---------------------------------------------------------------- Selbst-Update über den Server
def test_selbstupdate_fassungen():
    from taleward_worker import selbstupdate

    assert selbstupdate.neuer("0.2.0", "0.1.0") and not selbstupdate.neuer("0.1.0", "0.1.0")
    assert selbstupdate.neuer("0.10.0", "0.9.9") and not selbstupdate.neuer("kaputt", "0.1.0")


def test_selbstupdate_abfragen_und_laden(monkeypatch):
    import hashlib

    import httpx

    from taleward_worker import VERSION, selbstupdate

    inhalt = b"MZ neue Fassung"

    def antwort(req):
        if req.url.path == "/worker/v1/app-update":
            assert req.headers["Authorization"] == "Bearer t" and req.url.params["system"] in ("windows", "linux")
            return httpx.Response(200, json={"version": "9.9.9", "url": "https://srv/downloads/x.exe",
                                             "sha256": hashlib.sha256(inhalt).hexdigest(), "notes": "Neu"})
        return httpx.Response(200, content=inhalt)

    transport = httpx.MockTransport(antwort)
    echt_get, echt_stream = httpx.get, httpx.stream
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Client(transport=transport).get(*a, **{
        x: y for x, y in k.items() if x != "timeout"}))

    def stream(methode, url, **k):
        return httpx.Client(transport=transport).stream(methode, url, headers=k.get("headers"))

    monkeypatch.setattr(httpx, "stream", stream)
    angebot = selbstupdate.abfragen("https://srv", "t")
    assert angebot["version"] == "9.9.9" and selbstupdate.neuer(angebot["version"], VERSION)
    u = selbstupdate.Aktualisierung(angebot, "https://srv", "t")
    datei = u._laden()
    assert datei.read_bytes() == inhalt and u.anteil > 0
    u2 = selbstupdate.Aktualisierung({**angebot, "sha256": "0" * 64}, "https://srv", "t")
    with pytest.raises(RuntimeError, match="Prüfsumme"):
        u2._laden()
    assert not list((pfade.basis() / "updates").glob("*.exe"))
    assert echt_get and echt_stream


def test_anforderungen_aufteilen():
    text = "torch==2.8.0\ntorchcodec==0.7.0\nwhisperx==3.8.6\nnvidia-cudnn-cu12==9.10.2.21 ; sys_platform == 'linux'\n"
    basis, extra = motor.anforderungen_aufteilen(text)
    assert "torchcodec" not in basis and "whisperx==3.8.6" in basis and "nvidia-cudnn" in basis
    assert extra.strip() == "torchcodec==0.7.0"
    b2, e2 = motor.anforderungen_aufteilen("torch==2.8.0\n")
    assert e2 == "" and b2 == "torch==2.8.0\n"


def test_python_verknuepfung_verboten(tmp_path, monkeypatch):
    """Windows mit OneDrive & Co.: uv darf die Verknüpfung cpython-3.11-… nicht anlegen (os error 448)."""
    import sys

    from taleward_worker import motor, pfade

    monkeypatch.setattr(pfade, "basis", lambda: tmp_path)
    ordner = tmp_path / "python"
    for name in ("cpython-3.11.9-x", "cpython-3.11.16-x"):
        d = ordner / name
        exe = d / "python.exe" if sys.platform == "win32" else d / "bin" / "python3.11"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    (ordner / "cpython-3.11-x").symlink_to(ordner / "cpython-3.11.16-x")  # die Verknüpfung zählt nicht

    inst = motor.Installation.__new__(motor.Installation)
    inst.zeilen = []

    def uv_scheitert(*_a, **_k):
        raise motor.MotorFehler("installation", "error: Failed to create Python minor version link directory\n"
                                "  cause: ... (os error 448)")
    inst._uv = uv_scheitert
    assert "cpython-3.11.16-x" in str(inst._python_einrichten())
    # Verknüpfung weg, Ziel noch da; weitere uv-Aufrufe sehen den Python-Ordner nicht mehr
    assert not (ordner / "cpython-3.11-x").exists() and (ordner / "cpython-3.11.16-x").is_dir()
    assert inst._umgebung()["UV_PYTHON_INSTALL_DIR"] != str(ordner)

    def uv_anders(*_a, **_k):
        raise motor.MotorFehler("netz", "dns error")
    inst._uv = uv_anders
    try:
        inst._python_einrichten()
        raise AssertionError("anderer Fehler muss durchgereicht werden")
    except motor.MotorFehler as e:
        assert e.code == "netz"


def test_neue_serverfassung_tauscht_nur_den_code(eigener_ordner, monkeypatch):
    """Gleiche KI-Bibliotheken: kein neues 8-GB-Paket, nur der Taleward-Code wird getauscht."""
    import subprocess as sp

    py = pfade.motor_python()
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")
    (pfade.motor() / "taleward-motor.json").write_text(json.dumps({"fassung": "0.4.13", "backend": "cuda"}))
    monkeypatch.setattr(motor, "quelle", lambda f: {"ref": f"v{f}", "genau": True, "paket": "paket.zip",
                                                    "anforderungen": "egal"})
    monkeypatch.setattr(motor, "anforderungen_lesen", lambda q: "torch==2.8.0\nsix==1.17.0\n")
    monkeypatch.setattr(motor, "uv_programm", lambda: "uv")
    aufrufe = []

    def run(befehl, **kw):
        aufrufe.append(befehl)
        if "--dry-run" in befehl:
            return sp.CompletedProcess(befehl, 0, "Audited 2 packages\nWould make no changes\n", "")
        return sp.CompletedProcess(befehl, 0, "ok\n", "")

    monkeypatch.setattr(motor.subprocess, "run", run)
    uv = []
    monkeypatch.setattr(motor.Installation, "_uv", lambda self, *a, **k: uv.append(a))
    schritte = []
    inst = motor.Installation("0.4.14", vor_tausch=lambda: schritte.append("vor"),
                              nach_tausch=lambda: schritte.append("nach"))
    inst._lauf()
    assert inst.phase == "fertig" and schritte == ["vor", "nach"]
    assert uv == [("pip", "install", "--python", str(py), "--no-deps", "--reinstall", "paket.zip")]
    assert motor.installiert()["fassung"] == "0.4.14"

    # Geänderte Bibliotheken: der schnelle Weg wird nicht genommen
    monkeypatch.setattr(motor.subprocess, "run", lambda b, **k: sp.CompletedProcess(b, 0, "Would install 1 package", ""))
    assert motor.Installation("0.4.15")._nur_taleward_noetig(motor.quelle("0.4.15")) is False


def test_ki_paket_zeigt_auf_echten_python_ordner(eigener_ordner, monkeypatch):
    """Verweist das KI-Paket auf uvs Verknüpfung „cpython-3.11-…“ (die Windows mitunter sperrt oder uv neu anlegt),
    stellt die App es vor dem Start auf den echten Ordner um."""
    monkeypatch.setattr(motor.sys, "platform", "win32")
    echt = pfade.basis() / "python" / "cpython-3.11.16-windows-x86_64-none"
    echt.mkdir(parents=True)
    (echt / "python.exe").write_text("")
    pfade.motor().mkdir(parents=True, exist_ok=True)
    cfg = pfade.motor() / "pyvenv.cfg"
    cfg.write_text(f"home = {pfade.basis() / 'python' / 'cpython-3.11-windows-x86_64-none'}\nversion_info = 3.11\n")
    assert motor.reparieren() is True
    assert f"home = {echt}" in cfg.read_text() and "version_info = 3.11" in cfg.read_text()
    assert motor.reparieren() is False  # schon richtig


def test_kaputtes_ki_paket_startet_nicht_im_kreis(tmp_path):
    skript = tmp_path / "kaputt.py"
    skript.write_text("print('error: uv trampoline failed to spawn Python child process', flush=True)\n"
                      "import sys; sys.exit(1)\n")
    d = Dienst(Einstellungen(server="http://localhost:1", token="t"), befehl=[sys.executable, str(skript)])
    d.starten()
    assert _warten(lambda: d.zustand.get("code") == "motor_kaputt")
    time.sleep(1.5)
    assert not d.laeuft() and d.zustand["art"] == "fehler"
    assert any("neu installieren" in z for z in d.protokoll.zeilen)
