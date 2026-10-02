"""Freigabe (S3): Updates und KI-Paket nur mit gültiger Unterschrift, Repo fest, Download nur vom eigenen Server."""
import contextlib
import hashlib
import subprocess

import httpx
import pytest

from tests.freigabe_hilfe import freigabe, unterschreiben

ECHT_SCHLUESSEL = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIP6wpTFK9FF6IfXPsVJ5R2nFSQYW0RV+zi02WLlKS0JU test"
ECHT_TEXT = ("taleward-freigabe 1\nrepo Tinkerworkss/taleward-server\ntag v0.4.38\ncommit " + "a" * 40
             + "\ndatei " + "b" * 64 + " a.zip\n").encode()
ECHT_SIG = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAg/rClMUr0UXoh9c+xUnlHacVJBh
bRFX7OLTZYuUpLQlQAAAARdGFsZXdhcmQtZnJlaWdhYmUAAAAAAAAABnNoYTUxMgAAAFMA
AAALc3NoLWVkMjU1MTkAAABAetpQ108l5M3dX4dPHQynskJdaIquFoYpHOh5CT1w5+E9//
nwqMOihot7qT3bFYE/491me28LVrX43pd0wGXBDw==
-----END SSH SIGNATURE-----
"""
EXE = b"MZ setup " * 50
WORKER = "Tinkerworkss/taleward-worker"


def test_unterschrift_von_ssh_keygen(monkeypatch):
    from taleward_worker import freigabe as fg

    monkeypatch.setattr(fg, "OEFFENTLICHER_SCHLUESSEL", ECHT_SCHLUESSEL)
    assert fg.lesen(ECHT_TEXT, ECHT_SIG, "Tinkerworkss/taleward-server", "v0.4.38").commit == "a" * 40
    with pytest.raises(fg.FreigabeFehler):
        fg.lesen(ECHT_TEXT.replace(b"a.zip", b"b.zip"), ECHT_SIG, "Tinkerworkss/taleward-server", "v0.4.38")


def test_echter_schluessel_wie_im_server():
    from taleward_worker import freigabe as fg

    quelle = open(fg.__file__, encoding="utf-8").read()
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIP8G+vT3BsQPQ+D5UlN615BaEd1FCXEYcxO1KEf7u7Sd" in quelle


# ---------------------------------------------------------------- KI-Paket
def test_motor_nur_freigegebene_fassung(monkeypatch):
    from taleward_worker import freigabe as fg, motor

    gefragt = []

    def von_github(repo, tag, **k):
        gefragt.append((repo, tag))
        text, sig = freigabe(repo, tag, commit="c" * 40)
        return fg.lesen(text, sig, repo, tag)

    monkeypatch.setattr(fg, "von_github", von_github)
    q = motor.quelle("0.4.38")
    assert gefragt == [("Tinkerworkss/taleward-server", "v0.4.38")]
    assert q["paket"] == "https://github.com/Tinkerworkss/taleward-server/archive/" + "c" * 40 + ".zip"
    assert q["anforderungen"].endswith("/" + "c" * 40 + "/engine-requirements.txt")
    for boese in ["main", "../../x/y/main", "0.4", "0.4.38/../x", ""]:
        with pytest.raises(motor.MotorFehler) as e:
            motor.quelle(boese)
        assert e.value.code == "nicht_freigegeben"
    assert len(gefragt) == 1  # für ungültige Fassungen keine Anfrage

    def ohne(repo, tag, **k):
        raise fg.FreigabeFehler(f"Fassung {tag} ist nicht freigegeben")

    monkeypatch.setattr(fg, "von_github", ohne)
    with pytest.raises(motor.MotorFehler) as e:
        motor.quelle("0.4.39")
    assert e.value.code == "nicht_freigegeben"


# ---------------------------------------------------------------- Selbstupdate
class _Antwort:
    def __init__(self, inhalt):
        self.inhalt, self.headers = inhalt, {"content-length": str(len(inhalt))}

    def raise_for_status(self):
        pass

    def iter_bytes(self, n):
        yield self.inhalt


def _angebot(inhalt_freigabe=EXE, **zusatz):
    text, sig = freigabe(WORKER, "worker-v0.2.0", {"TalewardWorker-Setup.exe": inhalt_freigabe}, commit="e" * 40)
    return {"version": "0.2.0", "url": "https://boese.example/downloads/worker-windows/0.2.0/TalewardWorker-Setup.exe",
            "sha256": hashlib.sha256(EXE).hexdigest(), "freigabe": text.decode(), "freigabeSignatur": sig, **zusatz}


@pytest.fixture()
def abrufe(monkeypatch):
    from taleward_worker import selbstupdate

    liste = []

    @contextlib.contextmanager
    def stream(methode, url, **k):
        liste.append((url, k.get("headers", {}).get("Authorization")))
        yield _Antwort(EXE)

    monkeypatch.setattr(selbstupdate.httpx, "stream", stream)
    return liste


def test_installer_nur_vom_eigenen_server_und_mit_freigabe(abrufe):
    from taleward_worker import selbstupdate

    a = selbstupdate.Aktualisierung(_angebot(), "https://mein.server", "tok")
    assert a._laden().read_bytes() == EXE
    assert abrufe == [("https://mein.server/downloads/worker-windows/0.2.0/TalewardWorker-Setup.exe", "Bearer tok")]


def test_manipulierter_installer_mit_passender_serverpruefsumme_wird_verworfen(abrufe):
    from taleward_worker import selbstupdate

    a = selbstupdate.Aktualisierung(_angebot(inhalt_freigabe=b"echter installer"), "https://mein.server", "tok")
    with pytest.raises(RuntimeError, match="Freigabe"):
        a._laden()


@pytest.mark.parametrize("aenderung", [
    {"url": "https://boese.example/anderswo/x.exe"},
    {"freigabeSignatur": unterschreiben(b"etwas anderes")},
    {"version": "0.3.0"},  # Freigabe gehört zu 0.2.0
    {"version": "../x"},
])
def test_ungueltige_angebote(abrufe, aenderung):
    from taleward_worker import selbstupdate

    a = selbstupdate.Aktualisierung(_angebot(**aenderung), "https://mein.server", "tok")
    with pytest.raises(RuntimeError):
        a._laden()
    assert abrufe == []  # kein Abruf, Schlüssel geht nirgendwohin


def test_linux_repo_fest_und_commit_aus_freigabe(monkeypatch):
    from taleward_worker import selbstupdate

    befehle = []
    monkeypatch.setattr(selbstupdate.subprocess, "run",
                        lambda b, **k: befehle.append(b) or subprocess.CompletedProcess(b, 0, "", ""))
    text, sig = freigabe(WORKER, "worker-v0.2.0", commit="f" * 40)
    a = selbstupdate.Aktualisierung({"version": "0.2.0", "repo": "Boese/fremd", "tag": "worker-v9.9.9",
                                     "freigabe": text.decode(), "freigabeSignatur": sig}, "https://mein.server", "t")
    a._linux()
    assert befehle[0][-1] == f"taleward-worker[qt] @ https://github.com/{WORKER}/archive/{'f' * 40}.zip"


def test_linux_ohne_freigabe_vom_server_fragt_github(monkeypatch):
    from taleward_worker import freigabe as fg, selbstupdate

    def von_github(repo, tag, **k):
        raise fg.FreigabeFehler("nicht freigegeben")

    monkeypatch.setattr(fg, "von_github", von_github)
    monkeypatch.setattr(selbstupdate.subprocess, "run", lambda *a, **k: pytest.fail("darf nicht installieren"))
    with pytest.raises(RuntimeError, match="Freigabe"):
        selbstupdate.Aktualisierung({"version": "0.2.0", "tag": "worker-v0.2.0"}, "https://s", "t")._linux()
    assert httpx  # Import genutzt
