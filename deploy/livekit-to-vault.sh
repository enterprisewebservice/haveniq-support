#!/usr/bin/env bash
# livekit-to-vault.sh <lk project name> <kv path>
# Copies a LiveKit project's URL, API key and secret from the LiveKit CLI's own config
# (~/.livekit/cli-config.yaml, written by `lk cloud auth` or `lk project add`) into Vault, so nothing is
# retyped. Example:  deploy/livekit-to-vault.sh haven agent-office/haveniq-livekit
set -euo pipefail
proj="${1:?lk project name}"; path="${2:?kv path}"
here="$(cd "$(dirname "$0")" && pwd)"
vals=$(python3 - "$proj" <<'PY'
import sys, os, yaml
name = sys.argv[1]
cfg = yaml.safe_load(open(os.path.expanduser("~/.livekit/cli-config.yaml")))
for p in cfg.get("projects", []):
    if p.get("name") == name:
        print(f"LIVEKIT_URL={p['url']}\nLIVEKIT_API_KEY={p['api_key']}\nLIVEKIT_API_SECRET={p['api_secret']}")
        break
else:
    sys.exit(f"no project named {name!r} in ~/.livekit/cli-config.yaml (run: lk project list)")
PY
)
# shellcheck disable=SC2086
"$here/vault-put.sh" "$path" $vals
