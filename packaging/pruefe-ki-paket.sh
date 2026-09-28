#!/usr/bin/env bash
# Prüft, ob sich das KI-Paket genau so installieren lässt, wie es die Worker-App tut (für den GitHub-Ablauf):
# alles außer torchcodec über das PyTorch-Verzeichnis (CUDA 12.8), torchcodec von PyPI – für Windows und Linux.
set -euo pipefail
# Die Liste liegt im Server-Repository (TALEWARD_SERVER_REF: Branch oder Tag, Standard main)
ANF=/tmp/engine-requirements.txt
curl -fsSL "https://raw.githubusercontent.com/Tinkerworkss/taleward-server/${TALEWARD_SERVER_REF:-main}/engine-requirements.txt" -o "$ANF"
grep -viE '^torchcodec==' "$ANF" > /tmp/basis.txt
grep -iE '^torchcodec==' "$ANF" > /tmp/extra.txt || true
for plattform in x86_64-pc-windows-msvc x86_64-manylinux_2_28; do
  echo "== $plattform"
  uv pip compile /tmp/basis.txt --no-deps --python-version 3.11 --python-platform "$plattform" \
    --torch-backend cu128 -q -o "/tmp/$plattform.txt"
  grep -E "^(torch|torchaudio|ctranslate2|whisperx)==" "/tmp/$plattform.txt"
  if [ -s /tmp/extra.txt ]; then
    uv pip compile /tmp/extra.txt --no-deps --python-version 3.11 --python-platform "$plattform" -q \
      -o "/tmp/$plattform-extra.txt"
    grep -E "^torchcodec==" "/tmp/$plattform-extra.txt"
  fi
done
echo "KI-Paket auflösbar."
