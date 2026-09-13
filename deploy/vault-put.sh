#!/usr/bin/env bash
# vault-put.sh <kv path> KEY=VALUE [KEY=VALUE ...]
# Writes a KV v2 secret in the cluster's Vault. The Vault CLI inside the pod has no session of its own,
# so this logs in with the bootstrap root token read from the `vault-bootstrap-keys` Secret and passed over
# stdin (never as an argument, never printed). Run this on your own machine; nothing is echoed back.
set -euo pipefail
path="${1:?kv path, e.g. agent-office/haveniq-livekit}"; shift
[ $# -ge 1 ] || { echo "usage: $0 <kv path> KEY=VALUE [KEY=VALUE ...]" >&2; exit 2; }
tok=$(oc get secret vault-bootstrap-keys -n vault -o jsonpath='{.data.root-token}' | base64 -d)
args=$(printf '%q ' "$@")
printf '%s\n' "$tok" | oc exec -i -n vault vault-0 -- sh -c "read -r VAULT_TOKEN; export VAULT_TOKEN; vault kv put $path $args >/dev/null && echo \"written: $path\""
