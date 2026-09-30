"""KI-Paket („Motor“) installieren und aktualisieren.

Der Motor ist der Taleward-Server-Code in genau der Fassung des verbundenen Servers, mit den KI-Bibliotheken
(WhisperX, PyTorch mit CUDA). Die App installiert ihn mit uv in eine eigene Python-Umgebung im Benutzerordner:

1. Python 3.11 (von uv verwaltet, unabhängig von einem installierten Python)
2. Pakete aus engine-requirements.txt der passenden Fassung (feste Versionen wie auf dem Server getestet);
   PyTorch in der Variante für die vorhandene Grafikkarte (uv --torch-backend auto)
3. der Taleward-Code selbst (ohne Abhängigkeiten, die kamen aus Schritt 2) und ffmpeg (imageio-ffmpeg)

Im Testmodus reicht der Taleward-Code mit den Grundpaketen (ein paar MB statt einiger GB).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from taleward_worker import REPO, VERSION, pfade, prozesse

# Ungefähre Downloadgröße (für die Fortschrittsanzeige; gemessen an der Größe des Zwischenspeichers)
# Größe des installierten KI-Pakets in MB (gemessen: Windows mit CUDA 7,7 GB, Stand 0.4.4). Der Fortschritt zählt,
# wie weit der Zwischenspeicher von uv gewachsen ist – dort liegen die Pakete entpackt, also in dieser Größe.
# CUDA-Fassung von PyTorch. Fest statt „auto“: uv würde nach der Treiber-Fassung wählen und bei Treibern vor 560 auf
# einem Index landen, auf dem die festgeschriebene torch-Fassung gar nicht liegt (Installation scheitert oder wird
# stillschweigend Prozessor). cu128 läuft dank CUDA-Minor-Kompatibilität mit jedem Treiber ab 528; genau diese
# Fassung prüft auch der Bau (packaging/pruefe-ki-paket.sh).
TORCH_BACKEND = "cu128"

ERWARTET_MB = {"windows": 7900, "linux": 8500, "cpu": 1800, "test": 60}


class MotorFehler(Exception):
    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code
        self.text = text


def _ohne_fenster() -> dict:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def uv_programm() -> str:
    """uv liegt dem Windows-Paket bei (ressourcen/uv.exe); unter Linux installiert es das Installationsskript."""
    name = "uv.exe" if sys.platform == "win32" else "uv"
    mitgeliefert = pfade.ressourcen() / "bin" / name
    if mitgeliefert.exists():
        return str(mitgeliefert)
    gefunden = shutil.which("uv") or shutil.which(str(Path.home() / ".local" / "bin" / "uv"))
    if gefunden:
        return gefunden
    raise MotorFehler("uv_fehlt")


def _netzcode(e: httpx.HTTPError) -> str:
    from taleward_worker.server import zertifikatsfehler

    return "zertifikat" if zertifikatsfehler(e) else "github_nicht_erreichbar"


def quelle(fassung: str) -> dict:
    """Download-Adressen für eine Serverfassung. Gibt es kein passendes Git-Tag (v0.4.0), gilt der main-Zweig."""
    eigen = os.environ.get("TALEWARD_MOTOR_QUELLE")  # Entwicklung: lokaler Ordner des Server-Codes
    if eigen:
        return {"ref": "lokal", "genau": True, "paket": eigen,
                "anforderungen": str(Path(eigen) / "engine-requirements.txt")}
    kandidaten = [f"v{fassung}", fassung] if fassung and fassung != "0.0.0" else []
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": f"TalewardWorker/{VERSION}"}) as k:
        for ref in kandidaten:
            try:
                r = k.head(f"https://raw.githubusercontent.com/{REPO}/{ref}/engine-requirements.txt")
            except httpx.HTTPError as e:
                raise MotorFehler(_netzcode(e)) from None
            if r.status_code == 200:
                return _adressen(ref, genau=True)
    return _adressen("main", genau=False)


def _adressen(ref: str, genau: bool) -> dict:
    art = "tags" if ref != "main" else "heads"
    return {"ref": ref, "genau": genau,
            "paket": f"https://github.com/{REPO}/archive/refs/{art}/{ref}.zip",
            "anforderungen": f"https://raw.githubusercontent.com/{REPO}/{ref}/engine-requirements.txt"}


@dataclass
class Installation:
    """Läuft in einem eigenen Faden; die Oberfläche fragt den Stand ab."""
    fassung: str
    testmodus: bool = False
    backend: str = "cuda"         # "cuda" (NVIDIA) oder "cpu" (nur Prozessor, deutlich kleiner)
    phase: str = "start"          # start, python, pakete, taleward, ffmpeg, fertig, fehler, abgebrochen
    anteil: float = 0.0
    geladen_mb: int = 0
    erwartet_mb: int = 0
    fehler: str = ""
    fehlertext: str = ""
    zeilen: list[str] = field(default_factory=list)
    vor_tausch: object = None     # Aufruf vor dem Ersetzen des alten Motors (laufenden Worker anhalten)
    nach_tausch: object = None
    _prozess: subprocess.Popen | None = None
    _abbrechen: threading.Event = field(default_factory=threading.Event)

    def stand(self) -> dict:
        return {"phase": self.phase, "anteil": round(self.anteil, 3), "geladenMb": self.geladen_mb,
                "erwartetMb": self.erwartet_mb, "fehler": self.fehler, "fehlertext": self.fehlertext,
                "fassung": self.fassung, "letzteZeile": self.zeilen[-1] if self.zeilen else ""}

    def abbrechen(self) -> None:
        self._abbrechen.set()
        prozesse.beenden(self._prozess, sanft=False)

    # ------------------------------------------------------------------ Ablauf
    def starten(self) -> threading.Thread:
        t = threading.Thread(target=self._lauf, daemon=True, name="motor-installation")
        t.start()
        return t

    def _lauf(self) -> None:
        try:
            self._installieren()
            self.phase, self.anteil = "fertig", 1.0
        except MotorFehler as e:
            self.phase = "abgebrochen" if self._abbrechen.is_set() else "fehler"
            self.fehler, self.fehlertext = e.code, e.text
        except Exception as e:  # unerwartet – Text für das Protokoll
            self.phase, self.fehler, self.fehlertext = "fehler", "unerwartet", f"{type(e).__name__}: {e}"
        finally:
            shutil.rmtree(pfade.basis() / "cache", ignore_errors=True)  # spart einige GB

    def _umgebung(self) -> dict:
        env = saubere_umgebung()
        env.update({"UV_NO_CONFIG": "1", "UV_NATIVE_TLS": "1",  # Zertifikate aus dem Windows-Speicher (Virenscanner)
                    "UV_CACHE_DIR": str(pfade.basis() / "cache"),
                    "UV_PYTHON_INSTALL_DIR": str(pfade.basis() / "python"),
                    "UV_PYTHON_PREFERENCE": "only-managed",
                    "UV_NO_PROGRESS": "1", "NO_COLOR": "1", "UV_LINK_MODE": "hardlink"})  # kein zweites Exemplar: spart während der Installation ~8 GB
        if getattr(self, "_ohne_verknuepfung", False):
            # Python liegt dann „außerhalb“ von uv: uv soll den Python-Ordner mit der blockierten Verknüpfung
            # nicht mehr ansehen (sonst os error 448 bei jeder Abfrage des Interpreters)
            env["UV_PYTHON_INSTALL_DIR"] = str(pfade.basis() / "python-ohne-uv")
            env["UV_PYTHON_PREFERENCE"] = "managed"
        return env

    def _uv(self, *argumente: str, anteil_von: float, anteil_bis: float, erwartet_mb: int = 0,
            messordner: Path | None = None) -> None:
        if self._abbrechen.is_set():
            raise MotorFehler("abgebrochen")
        befehl = [uv_programm(), *argumente]
        self.zeilen.append("$ uv " + " ".join(argumente))
        cache = messordner or pfade.basis() / "cache"   # dessen Wachstum zeigt den Fortschritt
        start_mb = pfade.ordnergroesse(cache) // 2 ** 20 if cache.exists() else 0
        self._prozess = subprocess.Popen(befehl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", env=self._umgebung(),
                                         cwd=pfade.basis(), **_ohne_fenster())
        prozesse.zuordnen(self._prozess)
        lesen = threading.Thread(target=self._mitlesen, args=(self._prozess,), daemon=True)
        lesen.start()
        while self._prozess.poll() is None:
            if erwartet_mb:
                self.geladen_mb = max(0, pfade.ordnergroesse(cache) // 2 ** 20 - start_mb) if cache.exists() else 0
                self.anteil = anteil_von + (anteil_bis - anteil_von) * min(0.97, self.geladen_mb / erwartet_mb)
            time.sleep(1)
        lesen.join(2)
        if self._prozess.returncode != 0:
            if self._abbrechen.is_set():
                raise MotorFehler("abgebrochen")
            letzte = "\n".join(self.zeilen[-12:])
            raise MotorFehler(fehlercode(letzte), letzte)
        self.anteil = anteil_bis

    def _mitlesen(self, p: subprocess.Popen) -> None:
        for zeile in p.stdout:
            zeile = zeile.rstrip()
            if zeile:
                self.zeilen.append(zeile)
                del self.zeilen[:-400]

    def _installieren(self) -> None:
        q = quelle(self.fassung)
        self.zeilen.append(f"Fassung {self.fassung} → {q['ref']}" + ("" if q["genau"] else " (kein Tag, main)"))
        if not self.testmodus:
            art, pakete = self._aenderung(q)
            if art == "gleich":
                return self._nur_taleward(q)
            if art == "klein":  # nur kleine Bibliotheken neu – in den vorhandenen Motor, statt 8 GB neu zu laden
                return self._nur_taleward(q, zusatz=pakete)
        system = "test" if self.testmodus else "cpu" if self.backend == "cpu" else (
            "windows" if sys.platform == "win32" else "linux")
        self.erwartet_mb = ERWARTET_MB[system]
        neu = pfade.basis() / "motor-neu"
        shutil.rmtree(neu, ignore_errors=True)

        self.phase = "python"
        basis_python = self._python_einrichten()
        self._uv("venv", str(neu), "--python", str(basis_python), anteil_von=0.05, anteil_bis=0.06)
        home_festlegen(neu, basis_python)
        py = str(neu / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))

        self.phase = "pakete"
        if self.testmodus:
            self._uv("pip", "install", "--python", py, q["paket"], "imageio-ffmpeg",
                     anteil_von=0.06, anteil_bis=0.95, erwartet_mb=self.erwartet_mb)
        else:
            torch = "cpu" if self.backend == "cpu" else TORCH_BACKEND
            basis, extra = anforderungen_aufteilen(anforderungen_lesen(q["anforderungen"]))
            ordner = pfade.basis() / "cache"
            ordner.mkdir(parents=True, exist_ok=True)
            (ordner / "basis.txt").write_text(basis, encoding="utf-8")
            (ordner / "extra.txt").write_text(extra, encoding="utf-8")
            # Alle Versionen stehen fest (aus uv.lock) → ohne erneutes Auflösen installieren
            self._uv("pip", "install", "--python", py, "--no-deps", "--torch-backend", torch,
                     "-r", str(ordner / "basis.txt"), "imageio-ffmpeg",
                     anteil_von=0.06, anteil_bis=0.88, erwartet_mb=self.erwartet_mb)
            if extra.strip():
                self._uv("pip", "install", "--python", py, "--no-deps", "-r", str(ordner / "extra.txt"),
                         anteil_von=0.88, anteil_bis=0.9)
            self.phase = "taleward"
            self._uv("pip", "install", "--python", py, "--no-deps", q["paket"], anteil_von=0.9, anteil_bis=0.95)

        self.phase = "ffmpeg"
        pruefung = _python_pruefen(py, "import app, sys; print('ok')")
        if "ok" not in pruefung.stdout:
            raise MotorFehler("installation", pruefung.stderr[-2000:])
        cuda = self.backend != "cpu" and not self.testmodus
        ist = torch_pruefen(py) if cuda else {}
        if cuda and ist.get("cuda") is None:  # keine CUDA-Fassung gelandet – nicht als Grafikkarten-Motor eintragen
            raise MotorFehler("installation", ist.get("fehler") or "PyTorch wurde ohne CUDA installiert.")

        # ffmpeg noch aus dem neuen Ordner heraus einrichten – scheitert das, ist der alte Motor unberührt
        ffmpeg_einrichten(Path(py))
        kennzeichnung = {"fassung": self.fassung, "ref": q["ref"], "testmodus": self.testmodus,
                         "backend": self.backend, "torch": ist, "installiert": time.strftime("%Y-%m-%d %H:%M")}
        (neu / "taleward-motor.json").write_text(json.dumps(kennzeichnung), encoding="utf-8")

        # Erst jetzt den alten Motor ersetzen – bei einem Fehler bleibt der bisherige nutzbar
        alt = pfade.motor()
        weg = pfade.basis() / "motor-alt"
        ordner_entfernen(weg)
        if callable(self.vor_tausch):
            self.vor_tausch()
        self.phase = "tauschen"
        try:
            if alt.exists():
                umbenennen(alt, weg)
            umbenennen(neu, alt)
        except BaseException:
            self.phase = "fehler"  # damit nach_tausch die alte Fassung stehen lässt
            raise
        finally:
            if callable(self.nach_tausch):
                self.nach_tausch()
        ordner_entfernen(weg)
        # Hinweis: Skripte im Motor (chronik.exe) kennen noch den alten Ordnernamen – gestartet wird daher immer
        # mit „python -m app.cli“, das funktioniert nach dem Umbenennen weiter.


    # ------------------------------------------------------------------ Schneller Weg bei neuer Serverfassung
    def _nur_taleward_noetig(self, q: dict) -> bool:
        return self._aenderung(q)[0] == "gleich"

    def _aenderung(self, q: dict) -> tuple[str, list[str]]:
        """Was hat sich an den KI-Bibliotheken gegenüber dem installierten Motor geändert? Probelauf von uv:
        „gleich“ (nur Taleward-Code tauschen, Sekunden), „klein“ (ein paar kleine Pakete – in den vorhandenen Motor)
        oder „gross“ (torch/CUDA anders – vollständige Installation, 8 GB)."""
        inst = installiert()
        if not inst or inst.get("testmodus") or inst.get("backend", "cuda") != self.backend:
            return "gross", []
        try:
            basis, extra = anforderungen_aufteilen(anforderungen_lesen(q["anforderungen"]))
            ordner = pfade.basis() / "cache"
            ordner.mkdir(parents=True, exist_ok=True)
            (ordner / "pruefen.txt").write_text(basis + "\n" + extra, encoding="utf-8")
            torch = "cpu" if self.backend == "cpu" else TORCH_BACKEND
            res = subprocess.run([uv_programm(), "pip", "install", "--dry-run", "--python", str(pfade.motor_python()),
                                  "--no-deps", "--torch-backend", torch, "-r", str(ordner / "pruefen.txt")],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                                 env=self._umgebung(), cwd=pfade.basis(), **_ohne_fenster())
        except (MotorFehler, OSError, subprocess.SubprocessError):
            return "gross", []
        ausgabe = res.stdout + res.stderr
        if res.returncode != 0:
            self.zeilen.append("Probelauf gescheitert – vollständige Installation.")
            return "gross", []
        if "Would make no changes" in ausgabe:
            self.zeilen.append("KI-Bibliotheken unverändert – nur der Taleward-Code wird getauscht.")
            return "gleich", []
        pakete = geaenderte_pakete(ausgabe)
        if pakete and not any(schwer(p) for p in pakete):
            self.zeilen.append("Kleine Änderung an den KI-Bibliotheken (" + ", ".join(pakete) +
                               ") – wird in das vorhandene KI-Paket eingespielt.")
            return "klein", pakete
        self.zeilen.append("KI-Bibliotheken haben sich geändert – vollständige Installation.")
        return "gross", pakete

    def _nur_taleward(self, q: dict, zusatz: list[str] | None = None) -> None:
        self.phase = "taleward"
        py = str(pfade.motor_python())
        if callable(self.vor_tausch):
            self.vor_tausch()  # laufenden Worker anhalten (nur für wenige Sekunden)
        try:
            if zusatz:
                self.phase = "pakete"
                torch = "cpu" if self.backend == "cpu" else TORCH_BACKEND
                self._uv("pip", "install", "--python", py, "--no-deps", "--torch-backend", torch,
                         "-r", str(pfade.basis() / "cache" / "pruefen.txt"), anteil_von=0.1, anteil_bis=0.6)
                self.phase = "taleward"
            self._uv("pip", "install", "--python", py, "--no-deps", "--reinstall", q["paket"],
                     anteil_von=0.6 if zusatz else 0.1, anteil_bis=0.9)
            pruefung = _python_pruefen(py, "import app, sys; print('ok')")
            if "ok" not in pruefung.stdout:
                raise MotorFehler("installation", pruefung.stderr[-2000:])
            daten = installiert() or {}
            daten.update({"fassung": self.fassung, "ref": q["ref"], "installiert": time.strftime("%Y-%m-%d %H:%M")})
            (pfade.motor() / "taleward-motor.json").write_text(json.dumps(daten), encoding="utf-8")
        except BaseException:
            self.phase = "fehler"  # damit nach_tausch die alte Fassung stehen lässt
            raise
        finally:
            if callable(self.nach_tausch):
                self.nach_tausch()

    def _python_einrichten(self) -> Path:
        """Python 3.11 über uv holen und den Pfad zur python(.exe) liefern.

        uv legt zusätzlich eine Verknüpfung „cpython-3.11-…“ an (unter Windows eine Junction). Manche Windows-PCs
        verbieten das (os error 448, „nicht vertrauenswürdiger Bereitstellungspunkt“, z. B. mit OneDrive „Dateien
        bei Bedarf“). Python selbst ist dann trotzdem vollständig da – wir nehmen es direkt, ohne die Verknüpfung.
        """
        ordner = pfade.basis() / "python"
        vorhanden = python_finden(ordner)
        if vorhanden is not None and python_laeuft(vorhanden):
            # Nicht neu installieren: uv würde dabei die Verknüpfung „cpython-3.11-…“ anfassen, an der ein älteres,
            # noch laufendes KI-Paket hängen kann
            self.zeilen.append(f"Python 3.11 vorhanden: {vorhanden.parent.name}")
            self._ohne_verknuepfung = True  # uv soll den Python-Ordner (samt Verknüpfung) gar nicht erst durchsuchen
            return vorhanden
        try:
            self._uv("python", "install", "3.11", "--no-bin", "--no-registry", anteil_von=0.0, anteil_bis=0.05, erwartet_mb=110,
                     messordner=ordner)
        except MotorFehler as e:
            if e.code == "abgebrochen" or not verknuepfung_verboten(e.text):
                raise
            self.zeilen.append("Hinweis: Windows erlaubt die Python-Verknüpfung nicht – nutze Python direkt.")
            verknuepfungen_entfernen(ordner)
            self._ohne_verknuepfung = True
        gefunden = python_finden(ordner)
        if gefunden is None:
            raise MotorFehler("installation", "\n".join(self.zeilen[-12:]) + "\nPython 3.11 nicht gefunden.")
        return gefunden


def verknuepfung_verboten(text: str) -> bool:
    return "os error 448" in text or "minor version link" in text


def verknuepfungen_entfernen(ordner: Path) -> None:
    """Die (blockierte) Verknüpfung „cpython-3.11-…“ entfernen – nur die Verknüpfung, nie das Ziel."""
    for d in ordner.glob("cpython-3.11-*"):
        try:
            if d.is_symlink():
                d.unlink()
            elif getattr(d, "is_junction", lambda: False)() or (sys.platform == "win32" and _ist_umleitung(d)):
                os.rmdir(d)   # entfernt bei einer Junction nur die Verknüpfung selbst
        except OSError:
            pass


def _ist_umleitung(d: Path) -> bool:
    try:
        return bool(os.lstat(d).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def python_laeuft(python: Path) -> bool:
    try:
        return subprocess.run([str(python), "-c", "print(1)"], capture_output=True, text=True, timeout=60,
                              **_ohne_fenster()).stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def home_festlegen(venv: Path, python: Path) -> None:
    """pyvenv.cfg: „home“ auf den echten Python-Ordner statt auf uvs Verknüpfung „cpython-3.11-…“. Die Verknüpfung
    blockiert Windows manchmal (os error 448) oder legt sie bei einer Neuinstallation neu an – das KI-Paket fände sein
    Python dann nicht mehr („uv trampoline failed to spawn Python child process“)."""
    cfg = venv / "pyvenv.cfg"
    try:
        zeilen = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    neu = [f"home = {python.parent}" if z.strip().lower().startswith("home") and "=" in z else z for z in zeilen]
    if neu != zeilen:
        cfg.write_text("\n".join(neu) + "\n", encoding="utf-8")


def reparieren() -> bool:
    """Vor jedem Start: Zeigt das KI-Paket auf ein Python, das es nicht (mehr) gibt, auf das vorhandene umstellen.
    True, wenn etwas geändert wurde."""
    cfg = pfade.motor() / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return False
    home = next((z.split("=", 1)[1].strip() for z in text.splitlines() if z.strip().lower().startswith("home")), "")
    exe = "python.exe" if sys.platform == "win32" else "bin/python3.11"
    echt = python_finden(pfade.basis() / "python") or python_finden(pfade.basis() / "python-ohne-uv")
    if echt is None or (home and Path(home) == echt.parent and (Path(home) / exe).is_file()):
        return False
    if home and (Path(home) / exe).is_file() and "cpython-3.11-" not in Path(home).name:
        return False  # zeigt schon auf einen echten, vorhandenen Ordner
    home_festlegen(pfade.motor(), echt)
    return True


def python_finden(ordner: Path) -> Path | None:
    """Neueste von uv installierte Python-3.11-Fassung (nicht die Verknüpfung „cpython-3.11-…“)."""
    kandidaten = []
    for d in ordner.glob("cpython-3.11.*"):
        exe = d / "python.exe" if sys.platform == "win32" else d / "bin" / "python3.11"
        teile = d.name.split("-")[1].split(".")
        if exe.is_file() and len(teile) == 3 and teile[2].isdigit():
            kandidaten.append((int(teile[2]), exe))
    return max(kandidaten)[1] if kandidaten else None


# Pakete, die es im PyTorch-Verzeichnis nicht für jedes System gibt (torchcodec cu128: nur Linux). Sie kommen von
# PyPI in der gewöhnlichen Fassung – Taleward übergibt Audio im Speicher, torchcodec dekodiert nichts.
OHNE_TORCH_VERZEICHNIS = ("torchcodec",)


def anforderungen_lesen(quelle: str) -> str:
    if quelle.startswith(("http://", "https://")):
        try:
            r = httpx.get(quelle, timeout=30, follow_redirects=True, headers={"User-Agent": f"TalewardWorker/{VERSION}"})
        except httpx.HTTPError as e:
            raise MotorFehler(_netzcode(e)) from None
        if r.status_code != 200:
            raise MotorFehler("installation", f"engine-requirements.txt: HTTP {r.status_code}")
        return r.text
    return Path(quelle).read_text(encoding="utf-8")


def anforderungen_aufteilen(text: str) -> tuple[str, str]:
    """engine-requirements.txt → (über das PyTorch-Verzeichnis, direkt von PyPI)."""
    basis, extra = [], []
    for zeile in text.splitlines():
        name = zeile.split("==")[0].split(";")[0].strip().lower()
        (extra if name in OHNE_TORCH_VERZEICHNIS else basis).append(zeile)
    return "\n".join(basis) + "\n", "\n".join(extra) + ("\n" if extra else "")


def ffmpeg_einrichten(python: Path) -> Path:
    """ffmpeg aus imageio-ffmpeg als „ffmpeg(.exe)“ in den Werkzeug-Ordner legen (WhisperX ruft „ffmpeg“ auf)."""
    aus = _python_pruefen(str(python), "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())", timeout=60)
    quelle_pfad = Path(aus.stdout.strip().splitlines()[-1]) if aus.stdout.strip() else Path("fehlt")
    if aus.returncode != 0 or not quelle_pfad.exists():
        raise MotorFehler("ffmpeg", aus.stderr[-1000:])
    ziel = pfade.werkzeuge() / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    ziel.unlink(missing_ok=True)
    shutil.copy2(quelle_pfad, ziel)
    if sys.platform != "win32":
        ziel.chmod(0o755)
    return ziel


# Umgebungsvariablen, die fremde Python- oder uv-Installationen hinterlassen und den Motor verbiegen würden:
# PYTHONHOME (Anaconda-Altlast → „Fatal Python error: init_fs_encoding“), PYTHONPATH (fremdes numpy/torch),
# UV_*/PIP_* (andere Indexe, Konfigurationsdateien, Offline-Modus).
_FREMDE_VARIABLEN = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONSAFEPATH",
                     "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PIP_REQUIRE_VIRTUALENV")


def saubere_umgebung() -> dict:
    """Kopie der Umgebung ohne alles, was Python oder uv von außen umstellt."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in _FREMDE_VARIABLEN and not k.upper().startswith(("UV_", "PIP_"))}
    return env


def _python_pruefen(py: str, code: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Kurzer Probelauf im Motor – immer UTF-8, unabhängig von der Windows-Codepage und von Umgebungsvariablen des
    Benutzers (PYTHONHOME, PYTHONPATH …), die den Motor sonst aus dem Tritt bringen."""
    env = saubere_umgebung()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return subprocess.run([py, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env, **_ohne_fenster())


def torch_pruefen(py: str) -> dict:
    """Was ist wirklich drin? {"cuda": "12.8" | None, "sichtbar": bool, "karte": str | None} – oder {"fehler": …}."""
    code = ("import json, torch; v = torch.version.cuda; s = bool(v) and torch.cuda.is_available(); "
            "print(json.dumps({'cuda': v, 'sichtbar': s, 'karte': torch.cuda.get_device_name(0) if s else None}))")
    try:
        r = _python_pruefen(py, code, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return {"fehler": f"{type(e).__name__}: {e}"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"fehler": (r.stderr or r.stdout)[-1000:].strip() or "PyTorch antwortet nicht."}


SCHWERE_PAKETE = ("torch", "torchaudio", "torchvision", "nvidia-", "triton", "ctranslate2", "onnxruntime",
                  "cudnn", "cublas", "cuda-")


def schwer(paket: str) -> bool:
    name = paket.lower().split("==")[0].strip()
    return any(name.startswith(s) or name == s.rstrip("-") for s in SCHWERE_PAKETE)


def geaenderte_pakete(ausgabe: str) -> list[str]:
    """Aus dem uv-Probelauf („ + paket==1.0“, „ - paket==0.9“) die betroffenen Paketnamen."""
    namen: list[str] = []
    for zeile in ausgabe.splitlines():
        z = zeile.strip()
        if z[:2] in ("+ ", "- ") and len(z) > 2:
            name = z[2:].split("==")[0].split(" ")[0].strip()
            if name and name not in namen:
                namen.append(name)
    return namen


PLATZ_RESERVE_GB = 3  # Luft für Windows und die Aufnahme, die gerade bearbeitet wird


def platz_fehlt_gb(testmodus: bool, backend: str) -> float:
    """Wie viele GB fehlen für die Installation? 0, wenn genug frei ist. Beim Aktualisieren liegt der alte Motor
    noch da, und uv hält die entpackten Pakete im Zwischenspeicher – Spitze etwa das Doppelte des Pakets."""
    system = "test" if testmodus else "cpu" if backend == "cpu" else ("windows" if sys.platform == "win32" else "linux")
    noetig_gb = ERWARTET_MB[system] / 1024 * 2 + PLATZ_RESERVE_GB
    try:
        frei_gb = shutil.disk_usage(pfade.basis()).free / 1024 ** 3
    except OSError:
        return 0.0
    return max(0.0, noetig_gb - frei_gb)


def fehlercode(ausgabe: str) -> str:
    """Aus der letzten uv-Ausgabe einen Fehlercode für die Oberfläche machen (Texte in ui/texte.js, f_…)."""
    klein = ausgabe.lower()
    if any(s in klein for s in ("no space left", "speicherplatz", "not enough space", "os error 112")):
        return "kein_platz"
    if "os error 206" in klein or "filename or extension is too long" in klein or "zu lang" in klein:
        return "pfad_zu_lang"
    if any(s in klein for s in ("os error 5", "os error 32", "access is denied", "zugriff verweigert",
                                "being used by another process")):
        return "dateien_in_benutzung"
    if any(s in klein for s in ("dns error", "timed out", "failed to fetch", "connection reset",
                                "certificate", "tls handshake")):
        return "netz"
    return "installation"


def umbenennen(von: Path, nach: Path, versuche: int = 20, pause: float = 0.5) -> None:
    """Ordner umbenennen, unter Windows mit Geduld: direkt nach dem Beenden des Motors hält Windows Handles noch
    kurz offen, und der Virenscanner liest gerade die frisch geschriebenen Dateien – jede offene Datei lässt die
    Umbenennung mit „Zugriff verweigert“ scheitern. Nach den Versuchen ein sprechender Fehler statt „unerwartet“."""
    letzter: OSError | None = None
    for _ in range(max(1, versuche)):
        try:
            von.rename(nach)
            return
        except FileExistsError:
            ordner_entfernen(nach)
            letzter = FileExistsError(str(nach))
        except PermissionError as e:
            letzter = e
        except OSError as e:
            if getattr(e, "winerror", None) not in (5, 32, 145):  # Zugriff verweigert, in Benutzung, nicht leer
                raise
            letzter = e
        time.sleep(pause)
    raise MotorFehler("dateien_in_benutzung", f"{von.name} → {nach.name}: {letzter}")


def ordner_entfernen(p: Path) -> None:
    """rmtree, das auch schreibgeschützte Dateien (Windows) wegräumt und bei gesperrten nicht aufgibt."""
    if not p.exists():
        return

    def nachhelfen(funktion, pfad, _info):
        try:
            os.chmod(pfad, 0o700)
            funktion(pfad)
        except OSError:
            pass

    for _ in range(3):
        shutil.rmtree(p, onerror=nachhelfen)
        if not p.exists():
            return
        time.sleep(0.5)


def installiert() -> dict | None:
    try:
        d = json.loads((pfade.motor() / "taleward-motor.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if pfade.motor_python().exists() else None


def entfernen(modelle_auch: bool = False) -> None:
    for p in [pfade.motor(), pfade.basis() / "motor-neu", pfade.basis() / "motor-alt", pfade.basis() / "python",
              pfade.basis() / "python-ohne-uv", pfade.basis() / "cache", pfade.werkzeuge()]:
        ordner_entfernen(p)
    if modelle_auch:
        shutil.rmtree(pfade.modelle(), ignore_errors=True)
