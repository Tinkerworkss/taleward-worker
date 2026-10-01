"""Lokale Recaps: Ollama laden, prüfen, entpacken, starten und dem Worker bekanntmachen."""
import hashlib
import http.server
import io
import sys
import tarfile
import threading
import zipfile

import pytest

from taleward_worker import ollama

FAKE_OLLAMA = r'''#!{python}
# Nachgebautes Ollama: „ollama serve“ beantwortet /api/version auf OLLAMA_HOST.
import http.server, json, os, sys
host, port = os.environ["OLLAMA_HOST"].split(":")
assert sys.argv[1:] == ["serve"] and os.environ["OLLAMA_MODELS"] and os.environ["OLLAMA_KEEP_ALIVE"] == "2m"
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({{"version": "0.35.0"}}).encode())
    def log_message(self, *a): pass
http.server.HTTPServer((host, int(port)), H).serve_forever()
'''


def _programm() -> bytes:
    return FAKE_OLLAMA.format(python=sys.executable).encode()


def _zip() -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        info = zipfile.ZipInfo("bin/ollama")
        info.external_attr = 0o755 << 16
        z.writestr(info, _programm())
        z.writestr("lib/ollama/readme.txt", "Bibliotheken")
    return puffer.getvalue()


def _tar_zst() -> bytes:
    import zstandard

    roh = io.BytesIO()
    with tarfile.open(fileobj=roh, mode="w") as tar:
        daten = _programm()
        info = tarfile.TarInfo("bin/ollama")
        info.size, info.mode = len(daten), 0o755
        tar.addfile(info, io.BytesIO(daten))
    return zstandard.ZstdCompressor().compress(roh.getvalue())


@pytest.fixture()
def quelle(monkeypatch):
    """Kleiner HTTP-Server, der die Archive ausliefert."""
    dateien: dict[str, bytes] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            inhalt = dateien.get(self.path.rsplit("/", 1)[-1])
            if inhalt is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(inhalt)))
            self.end_headers()
            self.wfile.write(inhalt)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(ollama, "QUELLE", f"http://127.0.0.1:{srv.server_address[1]}")
    yield dateien
    srv.shutdown()


def _installieren(monkeypatch, quelle, name, inhalt, sha=None):
    quelle[name] = inhalt
    monkeypatch.setattr(ollama, "DATEIEN", {"linux": (name, sha or hashlib.sha256(inhalt).hexdigest()),
                                            "windows": (name, sha or hashlib.sha256(inhalt).hexdigest())})
    inst = ollama.Installation()
    inst.starten().join(30)
    return inst


@pytest.mark.skipif(sys.platform == "win32", reason="nachgebautes Programm ist ein Python-Skript")
def test_laden_entpacken_starten(monkeypatch, quelle, tmp_path):
    monkeypatch.setattr(ollama, "EIGENER_PORT", 11499)
    monkeypatch.setattr(ollama, "FREMDE_ADRESSE", "http://127.0.0.1:9")  # kein fremdes Ollama
    inst = _installieren(monkeypatch, quelle, "ollama-linux-amd64.zip", _zip())
    assert inst.phase == "fertig", inst.fehlertext
    assert ollama.installiert() and ollama.programm().name == "ollama"
    assert not list(ollama.pfade.basis().glob("*.teil"))  # Download aufgeräumt

    dienst = ollama.OllamaDienst()
    try:
        assert dienst.adresse(False) == ollama.AUS
        assert dienst.adresse(True) == "http://127.0.0.1:11499" and dienst.laeuft()
        assert ollama.antwortet(dienst.eigene_adresse)
    finally:
        dienst.stoppen()
    assert not dienst.laeuft()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux-Archiv")
def test_linux_archiv_tar_zst(monkeypatch, quelle):
    inst = _installieren(monkeypatch, quelle, "ollama-linux-amd64.tar.zst", _tar_zst())
    assert inst.phase == "fertig", inst.fehlertext
    assert ollama.programm() is not None and ollama.installiert()


def test_falsche_pruefsumme_wird_verworfen(monkeypatch, quelle):
    inst = _installieren(monkeypatch, quelle, "ollama-linux-amd64.zip", _zip(), sha="0" * 64)
    assert inst.phase == "fehler" and "Prüfsumme" in inst.fehlertext
    assert not ollama.installiert() and not list(ollama.pfade.basis().glob("*.teil"))


def test_zip_mit_ausbruchspfad_wird_abgelehnt(monkeypatch, quelle):
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        z.writestr("../../boese.txt", "x")
    inst = _installieren(monkeypatch, quelle, "ollama-linux-amd64.zip", puffer.getvalue())
    assert inst.phase == "fehler" and "Unzulässiger Pfad" in inst.fehlertext


def test_vorhandenes_ollama_hat_vorrang(monkeypatch):
    monkeypatch.setattr(ollama, "antwortet", lambda url, zeit=2.0: url == ollama.FREMDE_ADRESSE)
    dienst = ollama.OllamaDienst()
    assert dienst.adresse(True) == ollama.FREMDE_ADRESSE and not dienst.laeuft()
    assert dienst.adresse(False) == ollama.AUS


def test_ohne_ollama_bleibt_der_worker_ohne_sprachmodell(monkeypatch):
    monkeypatch.setattr(ollama, "antwortet", lambda url, zeit=2.0: False)
    assert ollama.OllamaDienst().adresse(True) == ollama.AUS  # nicht installiert, nichts läuft


def test_motor_umgebung_nennt_ollama(monkeypatch):
    from taleward_worker.dienst import Dienst
    from taleward_worker.einstellungen import Einstellungen

    monkeypatch.setattr(ollama, "antwortet", lambda url, zeit=2.0: url == ollama.FREMDE_ADRESSE)
    e = Einstellungen(server="https://x", token="t")
    d = Dienst(e, befehl=["true"])
    assert d._motor_umgebung()["WORKER_LLM_URL"] == ollama.AUS  # aus: auch ein fremdes Ollama nicht nutzen
    e.recaps_lokal = True
    assert d._motor_umgebung()["WORKER_LLM_URL"] == ollama.FREMDE_ADRESSE
    e.testmodus = True
    assert d._motor_umgebung()["WORKER_LLM_URL"] == ollama.AUS
