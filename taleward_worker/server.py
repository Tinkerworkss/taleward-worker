"""Verbindung zum Taleward-Server: Adresse prüfen, koppeln, Fassung abfragen."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from taleward_worker import VERSION


class ServerFehler(Exception):
    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code
        self.text = text


def _klient() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20), headers={"User-Agent": f"TalewardWorker/{VERSION}"},
                        follow_redirects=False)


def adresse_normalisieren(eingabe: str) -> str:
    a = eingabe.strip().rstrip("/")
    if not a:
        raise ServerFehler("adresse_leer")
    if not re.match(r"^https?://", a, re.I):
        a = "https://" + a
    teile = urlparse(a)
    if not teile.hostname:
        raise ServerFehler("adresse_ungueltig")
    return a


def unverschluesselt(adresse: str) -> bool:
    """HTTP außerhalb dieses PCs – nur mit ausdrücklicher Erlaubnis (Heimnetz)."""
    t = urlparse(adresse)
    return t.scheme == "http" and t.hostname not in ("localhost", "127.0.0.1", "::1")


def zertifikatsfehler(e: BaseException) -> bool:
    """Steckt hinter dem Verbindungsfehler ein abgelehntes Zertifikat? Typisch: Virenscanner mit HTTPS-Prüfung
    oder Firmen-Proxy schieben ein eigenes Stammzertifikat dazwischen; der Browser kennt es, wir sonst nicht."""
    import ssl

    ursache: BaseException | None = e
    for _ in range(5):
        if ursache is None:
            break
        if isinstance(ursache, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(ursache):
            return True
        ursache = ursache.__cause__ or ursache.__context__
    return False


def _verbindungsfehler(e: httpx.HTTPError) -> ServerFehler:
    if zertifikatsfehler(e):
        return ServerFehler("zertifikat")
    if isinstance(e, httpx.TimeoutException):
        return ServerFehler("zeitueberschreitung")
    if isinstance(e, httpx.ConnectError):
        return ServerFehler("nicht_erreichbar")
    return ServerFehler("kein_taleward")


def pruefen(adresse: str) -> dict:
    """Ist dort ein Taleward-Server? Liefert Name und API-Fassung."""
    try:
        with _klient() as k:
            r = k.get(f"{adresse}/api/v1/health")
            if r.status_code in (301, 302, 307, 308) and r.headers.get("location", "").startswith("https://"):
                raise ServerFehler("https_umleitung")
            if r.status_code != 200 or r.json().get("status") != "ok":
                raise ServerFehler("kein_taleward")
            info = k.get(f"{adresse}/api/v1/info")
            daten = info.json() if info.status_code == 200 else {}
    except httpx.HTTPError as e:
        raise _verbindungsfehler(e) from None
    except ValueError:
        raise ServerFehler("kein_taleward") from None
    return {"name": daten.get("serverName") or daten.get("name") or "", "apiVersion": daten.get("apiVersion", "")}


def koppeln(adresse: str, code: str, name: str) -> dict:
    code = code.strip().upper().replace(" ", "")
    if re.fullmatch(r"[A-Z0-9]{8}", code):
        code = f"{code[:4]}-{code[4:]}"
    try:
        with _klient() as k:
            r = k.post(f"{adresse}/worker/v1/pair", json={"code": code, "name": name.strip()[:60] or "worker"})
    except httpx.HTTPError as e:
        raise _verbindungsfehler(e) from None
    if r.status_code != 201:
        try:
            d = r.json()
            raise ServerFehler(d.get("code", "koppeln_fehlgeschlagen"), d.get("message", ""))
        except ValueError:
            raise ServerFehler("koppeln_fehlgeschlagen", r.text[:200]) from None
    return _json(r)


def _json(r: httpx.Response) -> dict:
    """Eine 200-Antwort ohne JSON (Hotel-Anmeldeseite, Proxy-Sperrseite, falsch gesetzter Reverse-Proxy) ist kein
    Taleward-Server – und kein Grund, den Hintergrund-Thread der App sterben zu lassen."""
    try:
        d = r.json()
    except ValueError:
        raise ServerFehler("kein_taleward") from None
    if not isinstance(d, dict):
        raise ServerFehler("kein_taleward")
    return d


def konfiguration(adresse: str, token: str) -> dict:
    """Einstellungen für Worker; enthält serverVersion (ab Server 0.4.0)."""
    try:
        with _klient() as k:
            r = k.get(f"{adresse}/worker/v1/config", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise _verbindungsfehler(e) from None
    if r.status_code == 401:
        raise ServerFehler("abgelehnt")
    if r.status_code != 200:
        raise ServerFehler("kein_taleward")
    return _json(r)
