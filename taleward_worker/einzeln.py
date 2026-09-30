"""Nur eine laufende App je Benutzer. Ein zweiter Start (z. B. Doppelklick auf die Verknüpfung) holt das
Fenster der laufenden App nach vorn und beendet sich. Der Installer bittet die laufende App über denselben Weg,
sich zu beenden („--beenden“), statt sie abzuschießen."""
from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Callable

from taleward_worker import pfade

GRUSS = b"taleward-worker:zeigen\n"
ABSCHIED = b"taleward-worker:beenden\n"
_MUTEX = "Local\\TalewardWorker-Instanz"
_mutex_handle = None


def _portdatei():
    return pfade.basis() / "instanz.port"


def _senden(nachricht: bytes, timeout: float = 1.5) -> bool:
    try:
        port = int(_portdatei().read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(nachricht)
            return s.recv(3) == b"ok\n"
    except OSError:
        return False


def andere_wecken() -> bool:
    """True, wenn schon eine App läuft (und sie das Fenster zeigt)."""
    return _senden(GRUSS)


def andere_beenden(warten_s: float = 15.0) -> bool:
    """Die laufende App bitten, sich zu beenden, und warten, bis sie weg ist. True, wenn keine (mehr) läuft."""
    if not _senden(ABSCHIED, timeout=3.0):
        return not andere_wecken()
    ende = time.monotonic() + warten_s
    while time.monotonic() < ende:
        if not _senden(GRUSS, timeout=0.5):
            return True
        time.sleep(0.3)
    return False


def sperren() -> bool:
    """Windows: benannter Mutex gegen das Rennen zweier gleichzeitiger Starts (Autostart und Doppelklick in
    derselben Sekunde – beide sähen noch keine Portdatei). True, wenn wir die erste Instanz sind."""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.CreateMutexW.restype = ctypes.c_void_p
        _mutex_handle = k32.CreateMutexW(None, True, _MUTEX)
        return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except (OSError, AttributeError):
        return True


def lauschen(zeigen: Callable[[], None], beenden: Callable[[], None] | None = None) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    _portdatei().write_text(str(srv.getsockname()[1]))

    def schleife():
        while True:
            try:
                verb, _ = srv.accept()
            except OSError:
                return
            with verb:
                verb.settimeout(2)
                try:
                    nachricht = verb.recv(64)
                    if nachricht == GRUSS:
                        verb.sendall(b"ok\n")
                        zeigen()
                    elif nachricht == ABSCHIED and beenden is not None:
                        verb.sendall(b"ok\n")
                        threading.Thread(target=beenden, daemon=True, name="beenden").start()
                    else:
                        verb.sendall(b"???")
                except OSError:
                    pass

    threading.Thread(target=schleife, daemon=True, name="einzeln").start()
    return srv
