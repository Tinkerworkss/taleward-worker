# PyInstaller: Taleward Worker als Ordner mit TalewardWorker(.exe). Aufruf im Hauptordner:
#   uv run pyinstaller packaging/taleward-worker.spec --noconfirm
# Unter Windows muss vorher bin/uv.exe liegen (lädt der GitHub-Ablauf herunter).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

wurzel = Path(SPECPATH).parent
windows = sys.platform == "win32"
daten = [(str(wurzel / "taleward_worker" / "ui"), "taleward_worker/ui")]
uv = wurzel / "bin" / ("uv.exe" if windows else "uv")
if uv.exists():
    daten.append((str(uv), "taleward_worker/bin"))

versteckt = collect_submodules("pystray") + ["webview.platforms.winforms", "webview.platforms.edgechromium"] \
    if windows else collect_submodules("pystray") + ["webview.platforms.qt"]

a = Analysis([str(wurzel / "taleward_worker" / "__main__.py")], pathex=[str(wurzel)], datas=daten,
             hiddenimports=versteckt, excludes=["tkinter", "matplotlib", "numpy"], noarchive=False)
pyz = PYZ(a.pure)

# Windows: Angaben zur Datei (Hersteller, Produkt, Fassung) – ohne sie wirkt eine .exe für Virenscanner verdächtiger
versionsinfo = None
if windows:
    import re
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)
    text = (wurzel / "taleward_worker" / "__init__.py").read_text(encoding="utf-8")
    fassung = re.search(r'^VERSION = "([0-9.]+)"', text, re.M).group(1)
    zahlen = tuple((list(map(int, fassung.split("."))) + [0, 0, 0, 0])[:4])
    versionsinfo = VSVersionInfo(
        ffi=FixedFileInfo(filevers=zahlen, prodvers=zahlen),
        kids=[StringFileInfo([StringTable("040704B0", [
            StringStruct("CompanyName", "Taleward"),
            StringStruct("FileDescription", "Taleward Worker"),
            StringStruct("FileVersion", fassung),
            StringStruct("InternalName", "TalewardWorker"),
            StringStruct("LegalCopyright", "AGPL-3.0 – https://github.com/Tinkerworkss/taleward-worker"),
            StringStruct("OriginalFilename", "TalewardWorker.exe"),
            StringStruct("ProductName", "Taleward Worker"),
            StringStruct("ProductVersion", fassung)])]),
              VarFileInfo([VarStruct("Translation", [0x0407, 1200])])])

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TalewardWorker", console=False,
          icon=str(wurzel / "packaging" / "windows" / "taleward-worker.ico"), upx=False, version=versionsinfo)
COLLECT(exe, a.binaries, a.datas, name="TalewardWorker", upx=False)
