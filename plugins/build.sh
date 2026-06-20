#!/usr/bin/env bash
# Package each plugin in this directory into dist/<id>.zip + a sibling
# dist/<id>.zip.sha256, ready to attach to a GitHub release and reference from
# the registry's plugins.json (downloadUrl / checksumUrl).
#
# The archive holds the runtime files at its root (plugin.json, __init__.py,
# README.md); tests/, CI, and caches are excluded. sshPilot verifies the
# SHA-256 before extracting.
set -euo pipefail
cd "$(dirname "$0")"

command -v zip >/dev/null || { echo "zip is required" >&2; exit 1; }
mkdir -p dist

for dir in sshpilot-*/; do
  [ -f "${dir}plugin.json" ] || continue
  id="$(python3 -c "import json;print(json.load(open('${dir}plugin.json'))['id'])")"
  out="dist/${id}.zip"
  rm -f "$out" "$out.sha256"
  ( cd "$dir" && zip -q -r -X "../$out" plugin.json __init__.py README.md )
  ( cd dist && sha256sum "${id}.zip" > "${id}.zip.sha256" )
  echo "built $out"
done
