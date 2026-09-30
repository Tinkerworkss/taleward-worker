"""Kindprozesse zuverlässig beenden – samt Enkeln.

Unter Windows beendet `Popen.kill()` nur den direkten Kindprozess. Der Motor ist aber ein uv-Trampolin, das den
echten Python-Interpreter startet, und der wiederum ffmpeg; Ollama startet eigene Runner-Prozesse mit mehreren GB
Grafikspeicher. Ohne Gegenmaßnahme überleben die alle: nach einem Absturz der App holt der alte Worker weiter
Aufträge, der Motor-Tausch scheitert an offenen DLLs, der Deinstaller lässt Gigabytes zurück.

Zwei Mittel:
- Alle Kinder kommen in ein Windows-Job-Objekt mit „kill on job close“: stirbt die App (auch durch Absturz oder
  taskkill), räumt Windows den ganzen Baum ab.
- `beenden()` beendet einen Prozess gezielt mit seinen Nachkommen (`taskkill /T`, unter Linux die Prozessgruppe).
"""
from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading

log = logging.getLogger("taleward_worker")

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_sperre = threading.Lock()
_job: int | None | bool = None  # None = noch nicht angelegt, False = nicht möglich


class _Grenzen(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _Zaehler(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]


class _ErweiterteGrenzen(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _Grenzen), ("IoInfo", _Zaehler), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _job_anlegen() -> int | bool:
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    grenzen = _ErweiterteGrenzen()
    grenzen.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(grenzen),
                                       ctypes.sizeof(grenzen)):
        k32.CloseHandle(job)
        return False
    return job  # bleibt absichtlich offen, bis der Prozess endet


def zuordnen(p: subprocess.Popen) -> bool:
    """Kindprozess ins Job-Objekt der App aufnehmen (Windows). Liefert False, wenn das nicht ging – dann bleibt
    `beenden()` als zweites Netz."""
    global _job
    if sys.platform != "win32":
        return False
    with _sperre:
        if _job is None:
            try:
                _job = _job_anlegen()
            except (OSError, AttributeError) as e:
                log.debug("Job-Objekt nicht möglich: %s", e)
                _job = False
        if not _job:
            return False
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = getattr(p, "_handle", None)
        eigener = False
        if handle is None:
            handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, p.pid)
            eigener = True
        if not handle:
            return False
        try:
            ok = bool(k32.AssignProcessToJobObject(_job, ctypes.c_void_p(int(handle))))
        finally:
            if eigener:
                k32.CloseHandle(handle)
        if not ok:
            log.debug("Prozess %s nicht ins Job-Objekt aufgenommen (Fehler %s)", p.pid, ctypes.get_last_error())
        return ok


def start_argumente() -> dict:
    """Zusätzliche Popen-Argumente: unter Windows kein Konsolenfenster, unter Linux eine eigene Prozessgruppe
    (damit `beenden` den ganzen Baum erwischt)."""
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}


def beenden(p: subprocess.Popen | None, warten: float = 10.0, sanft: bool = True) -> None:
    """Prozess mit allen Nachkommen beenden. `sanft`: erst freundlich (terminate/SIGTERM), nach `warten` Sekunden hart."""
    if p is None or p.poll() is not None:
        return
    if sanft:
        try:
            p.terminate()
        except OSError:
            pass
        try:
            p.wait(warten)
        except subprocess.TimeoutExpired:
            pass
    if p.poll() is None:
        _hart(p)
    else:
        _nachkommen(p)  # der Vater ist weg, die Kinder vielleicht nicht


def _hart(p: subprocess.Popen) -> None:
    if sys.platform == "win32":
        _taskkill(p.pid)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _nachkommen(p: subprocess.Popen) -> None:
    """Der Prozess selbst ist beendet – falls noch Kinder von ihm laufen (Windows), auch die."""
    if sys.platform == "win32":
        return  # der Baum hängt im Job-Objekt; taskkill auf eine tote PID träfe womöglich eine neue
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15,
                       creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        pass
