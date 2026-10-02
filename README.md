<p align="center"><img src="taleward_worker/ui/marke/taleward-mark.svg" alt="Taleward" width="96"></p>

# Taleward Worker

Die Worker-App verarbeitet Aufnahmen für einen [Taleward](https://taleward.org)-Server: Sie transkribiert, trennt die
Sprecher und schreibt auf Wunsch auch die Recaps – auf einem PC mit Grafikkarte, unter Windows oder Linux. Der Server
kann irgendwo laufen, etwa auf einem gemieteten Server oder einem Raspberry Pi. Der PC meldet sich von selbst bei ihm;
im Router muss nichts freigegeben werden.

Der Server liegt unter [taleward-server](https://github.com/Tinkerworkss/taleward-server).

## Installieren

### Windows

`TalewardWorker-Setup.exe` von der [Release-Seite](https://github.com/Tinkerworkss/taleward-worker/releases/latest)
herunterladen und starten. Administratorrechte sind nicht nötig.

- Windows 10 (ab 1809) oder 11
- am besten eine NVIDIA-Grafikkarte (ab 2 GB, gut ab 6 GB) mit aktuellem Treiber (ab 528); sonst arbeitet der Prozessor
- mindestens 8 GB Arbeitsspeicher
- etwa 16 GB freier Speicher (KI-Paket etwa 8 GB, Sprachmodell einige GB)

Die App liegt unter `%LOCALAPPDATA%\Programs\Taleward Worker`; KI-Paket, Modelle, Einstellungen und Protokoll unter
`%LOCALAPPDATA%\Taleward Worker`. Beim Deinstallieren fragt der Installer, ob diese Daten auch weg sollen.

### Linux

Für Ubuntu, Debian, Fedora und ähnliche Systeme (x86_64). Ohne `sudo` ausführen:

```bash
curl -fsSL https://raw.githubusercontent.com/Tinkerworkss/taleward-worker/main/install-linux.sh | bash
```

Das Skript installiert [uv](https://docs.astral.sh/uv/) und die App mit eigenem Fenster (Qt), legt einen Eintrag im
Anwendungsmenü an und fragt nach dem Autostart. Noch einmal ausgeführt, aktualisiert es die App. Die Daten liegen unter
`~/.local/share/taleward-worker`.

Unter GNOME gibt es ohne die Erweiterung „AppIndicator“ kein Symbol im Infobereich; das Fenster wird beim Schließen dann
nur minimiert.

## Einrichten

1. **Verbinden:** Adresse des Servers und Kopplungscode eingeben. Den Code erzeugt die Verwaltung des Servers unter
   **Transkription → Weiteren Worker anbinden**.
2. **Prüfen:** Die App prüft Grafikkarte, Treiber, Arbeitsspeicher und Speicherplatz und zeigt, wie sie arbeiten wird.
3. **KI-Paket laden:** einmalig, etwa 8 GB (Prozessor-Fassung deutlich kleiner). Beim ersten Auftrag kommen die
   Sprachmodelle dazu.
4. **Fertig:** Autostart, Hintergrundbetrieb und wie viel Grafikspeicher Taleward nutzen darf.

## Was die App kann

- **Passt sich der Hardware an.** Modell, Stapelgröße und Gerät wählt sie nach dem Grafikspeicher:

  | Grafikspeicher für Taleward | Arbeitsweise |
  |---|---|
  | ab 6 GB | `large-v3` komplett auf der Grafikkarte |
  | 3,5 bis 6 GB | `large-v3` mit kleinerem Stapel oder `large-v3-turbo` |
  | 2 bis 3,5 GB | Ausrichtung und Sprechertrennung auf dem Prozessor |
  | darunter oder ohne NVIDIA-Karte | alles auf dem Prozessor – grob 4–10 Stunden für 4 Stunden Aufnahme |

- **Schieberegler „Grafikspeicher für Taleward“:** legt fest, wie viel Grafikspeicher der Worker höchstens nutzt; der
  Rest bleibt für anderes. Nach jedem Auftrag zeigt die App Dauer und höchsten Grafikspeicher als gemessene Werte.
- **Immer passend zum Server:** Das KI-Paket wird in genau der Fassung des Servers installiert (der freigegebene Commit
  zum Git-Tag `v<Fassung>` im Server-Repository) und zieht nach einem Server-Update selbst nach.
- **Updates über den eigenen Server:** Die App fragt nur ihren Taleward-Server nach neuen Fassungen. Unter
  Windows installiert sie sie still, sobald sie nichts zu tun hat; unter Linux über `uv tool install`. Abschaltbar in
  den Einstellungen.
- **Nur freigegebene Fassungen:** App-Updates und KI-Paket werden nur installiert, wenn ihre Freigabe (`freigabe.txt`
  am Release, mit `ssh-keygen -Y sign` unterschrieben) zum fest eingebauten Schlüssel passt. Der Server reicht die
  Freigabe nur durch; Repo und Prüfsumme kommen aus ihr, der Installer nur von der Adresse des gekoppelten Servers.
- **Recaps auch hier schreiben** (freiwillig): Die App richtet Ollama in fester, geprüfter Fassung in ihrem
  Datenordner ein (etwa 1,4 GB, ohne Administratorrechte) und lädt beim ersten Recap ein Sprachmodell passend zur
  Grafikkarte (einige GB). Ein schon installiertes Ollama hat Vorrang, es muss mindestens Fassung 0.35.0 sein. Auf dem Server muss unter „Zusammenfassung“ „Lokales Modell“ gewählt sein.
- **Im Hintergrund:** Autostart, Symbol im Infobereich, Pausieren. Während eines Auftrags geht der PC nicht in den
  Ruhezustand.
- **Datenschutz:** Aufnahmen werden nur im Arbeitsordner verarbeitet und danach gelöscht. Zurück an den Server gehen
  nur Transkript, Hörproben und Stimmabdrücke.

**Geplant:** ein zweiter Motor auf Basis von whisper.cpp mit Vulkan für AMD- und Intel-Grafikkarten.

## Entwicklung

```bash
uv sync --extra qt          # Linux: Fenster über Qt; Windows nutzt WebView2
uv run pytest
TALEWARD_MOTOR=../taleward-server/.venv/bin/python uv run python -m taleward_worker   # Server-Umgebung als KI-Paket
```

- Die Oberfläche liegt in `taleward_worker/ui/` (HTML/CSS/JS, Texte Deutsch/Englisch in `texte.js`).
- Die App startet den Worker des Servers als `python -m app.cli worker --app`. Ereignisse kommen als JSON-Zeilen, die
  Steuerung (`pause`, `weiter`, `stopp`) geht über stdin – siehe `app/worker_app.py` im Server-Repository.
- `TALEWARD_MOTOR_QUELLE=<Ordner des Server-Codes>` installiert das KI-Paket aus einem lokalen Ordner statt von GitHub.
- `TALEWARD_WORKER_HOME` legt einen anderen Datenordner fest, z. B. für einen zweiten Worker auf demselben PC.
- Verpackung: `packaging/` (PyInstaller, Inno Setup für Windows, Symbole).

**Neue Fassung veröffentlichen:** Fassung in `pyproject.toml` erhöhen und auf GitHub ein Release mit dem Tag
`worker-v<Fassung>` anlegen. Der Ablauf „Bauen“ erzeugt den Windows-Installer und hängt ihn an das Release. Die Server
holen die neue Fassung innerhalb eines Tages, die Worker aktualisieren sich darüber selbst.

## Mitmachen

Fehler und Ideen gern als [Issue](https://github.com/Tinkerworkss/taleward-worker/issues). Die Regeln für Beiträge
gelten wie im Server-Repository: [CONTRIBUTING.md](https://github.com/Tinkerworkss/taleward-server/blob/main/CONTRIBUTING.md).

## Lizenz

AGPL-3.0, siehe [LICENSE](LICENSE). Name und Logo „Taleward“ sind nicht Teil der freien Lizenz des Quelltexts.
