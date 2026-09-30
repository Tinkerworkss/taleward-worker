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
