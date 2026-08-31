#!/usr/bin/env bash
# Install the Vault Radar plugin into an Obsidian vault.
#   ./install-obsidian-plugin.sh ~/path/to/vault
set -euo pipefail

VAULT="${1:-}"
if [ -z "$VAULT" ]; then
  echo "usage: $0 <vault-path>" >&2
  exit 1
fi
if [ ! -d "$VAULT" ]; then
  echo "no such directory: $VAULT" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)/obsidian-plugin"
DST="$VAULT/.obsidian/plugins/vault-radar"

if [ ! -d "$VAULT/.obsidian" ]; then
  echo "note: $VAULT has never been opened in Obsidian."
  echo "      Open it there once first, then re-run this script."
  exit 1
fi

mkdir -p "$DST"
cp "$SRC/manifest.json" "$SRC/main.js" "$DST/"
echo "installed -> $DST"

# Register the plugin so it is enabled on next launch.
CP="$VAULT/.obsidian/community-plugins.json"
python3 - "$CP" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    ids = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(ids, list):
        ids = []
except Exception:
    ids = []
if "vault-radar" not in ids:
    ids.append("vault-radar")
p.write_text(json.dumps(ids, indent=2), encoding="utf-8")
print("enabled  ->", p)
PY

echo
echo "Next:"
echo "  1. In Obsidian: Settings -> Community plugins -> turn OFF Restricted mode"
echo "  2. Reload the vault (Cmd+R) and confirm 'Vault Radar' is enabled"
echo "  3. Open the graph view and give your agent a prompt"
