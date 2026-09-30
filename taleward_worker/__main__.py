"""Start: taleward-worker [--hintergrund] [--pruefen]"""
from __future__ import annotations

import logging
import sys
import threading
import traceback


def main() -> int:
    argumente = sys.argv[1:]
    if "--pruefen" in argumente:
        # Für den Paketbau: alles laden, nichts anzeigen
        from taleward_worker import autostart, dienst, einzeln, hardware, motor, oberflaeche, pfade, prozesse, server  # noqa: F401
        import webview  # noqa: F401

        ui = pfade.ressourcen() / "ui" / "index.html"
        print("Taleward Worker ok:", ui.exists(), hardware.bericht()["system"])
        return 0 if ui.exists() else 1
    _fehler_sichtbar_machen()
    _systemzertifikate()
    try:
        from taleward_worker.oberflaeche import WorkerApp

        WorkerApp(hintergrund="--hintergrund" in argumente).run()
    except Exception:
        absturz_melden(traceback.format_exc())
        return 1
    return 0


def _fehler_sichtbar_machen() -> None:
    """Als Fensterprogramm gebaut hat die App keine Konsole: ohne diese Vorkehrungen verschwände jede Ausnahme
    spurlos – ein Doppelklick, nichts passiert, kein Protokoll."""
    try:
        from taleward_worker import pfade

        handler = logging.FileHandler(pfade.protokolle() / "app.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        wurzel = logging.getLogger()
        wurzel.addHandler(handler)
        wurzel.setLevel(logging.INFO)
    except OSError:
        pass

    def thread_fehler(args) -> None:
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        logging.getLogger("taleward_worker").error("Thread %s abgestürzt:\n%s", getattr(args.thread, "name", "?"), text)

    threading.excepthook = thread_fehler


def _systemzertifikate() -> None:
    """Zertifikate aus dem Speicher des Betriebssystems – Virenscanner mit HTTPS-Prüfung (Kaspersky, ESET, Avast …)
    und Firmen-Proxys schieben eigene Stammzertifikate dazwischen, die das mitgelieferte certifi-Bündel nicht kennt."""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as e:  # noqa: BLE001 – dann eben nur certifi
        logging.getLogger("taleward_worker").warning("Systemzertifikate nicht nutzbar: %s", e)


def absturz_melden(text: str) -> None:
    """Rückverfolgung in protokolle/absturz.log schreiben und – unter Windows – ein Hinweisfenster zeigen."""
    pfad = None
    try:
        from taleward_worker import pfade

        pfad = pfade.protokolle() / "absturz.log"
        with pfad.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    logging.getLogger("taleward_worker").error("Absturz:\n%s", text)
    if sys.platform == "win32":
        try:
            import ctypes

            wo = f"\n\nEinzelheiten: {pfad}" if pfad else ""
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None, f"Taleward Worker konnte nicht starten.{wo}\n\n{text[-600:]}", "Taleward Worker", 0x10)
        except Exception:
            pass
    elif sys.stderr:
        print(text, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
