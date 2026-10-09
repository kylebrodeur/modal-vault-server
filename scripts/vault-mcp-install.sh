#!/usr/bin/env bash
# vault-mcp-install.sh — install/remove the modal-vault MCP server entry for
# the operator's harnesses, via each harness's NATIVE surface where one
# exists (no hand-maintained config copies).
#
#   codex  -> `codex mcp add` (native CLI; token via --bearer-token-env-var)
#   claude -> Claude Code MCP config file (env expansion ${VAULT_API_TOKEN})
#   json   -> generic mcpServers JSON (omp harness-side mount, pi extension
#             import, any streamable-HTTP client)
#   gh     -> prints the copilot interactive guidance (copilot mcp is
#             interactive-only; no file to edit)
#
# The bearer token is supplied by the OPERATOR (env or --token) and is never
# printed or written except into the target config when that client format
# requires it (json mode; Claude file uses ${VAULT_API_TOKEN} expansion).
#
# Usage:
#   scripts/vault-mcp-install.sh --client codex  [--url URL] [--token TOKEN]
#   scripts/vault-mcp-install.sh --client claude [--url URL]
#   scripts/vault-mcp-install.sh --client json   [--url URL] [--token TOKEN]
#   scripts/vault-mcp-install.sh --client gh
#   scripts/vault-mcp-install.sh --client <any> --check    # dry-run
#   scripts/vault-mcp-install.sh --client <any> --remove   # remove the entry
set -euo pipefail

NAME="${VAULT_MCP_NAME:-modal-vault}"
CLIENT="${CLIENT:-}"
URL="${VAULT_MCP_URL:-https://<workspace>--modal-vault-server-serve.modal.run/mcp}"
TOKEN="${VAULT_API_TOKEN:-}"
CHECK=0
REMOVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --client) CLIENT="$2"; shift 2 ;;
    --url) URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --remove) REMOVE=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ -n "$CLIENT" ] || { echo "--client required (codex|claude|json|gh)" >&2; exit 2; }

# ---- codex: native CLI ------------------------------------------------------
if [ "$CLIENT" = "codex" ]; then
  if [ "$REMOVE" = "1" ]; then
    echo "== codex mcp remove $NAME =="
    [ "$CHECK" = "1" ] && exit 0
    exec codex mcp remove "$NAME"
  fi
  echo "== codex mcp add $NAME --url $URL --bearer-token-env-var VAULT_API_TOKEN =="
  [ "$CHECK" = "1" ] && exit 0
  # Native surface: codex reads the bearer from the env var at runtime,
  # so the token never lands in a file.
  exec codex mcp add "$NAME" --url "$URL" --bearer-token-env-var VAULT_API_TOKEN
fi

# ---- claude: config file with env expansion --------------------------------
if [ "$CLIENT" = "claude" ]; then
  CFG="${CLAUDE_MCP_CONFIG:-$HOME/.claude.json}"
  ENTRY=$(jq -n --arg url "$URL" \
    '{type:"http", url:$url, headers:{Authorization:"Bearer ${VAULT_API_TOKEN}"}}')
  if [ "$REMOVE" = "1" ]; then
    RESULT=$(jq --arg name "$NAME" 'del(.mcpServers[$name])' "$CFG" 2>/dev/null || echo '{"mcpServers":{}}')
    if [ "$CHECK" = "1" ]; then
      echo "== would write $CFG (entry removed) =="
      echo "$RESULT" | jq .
      exit 0
    fi
    umask 077
    printf '%s\n' "$RESULT" > "$CFG"
    echo "removed $NAME from $CFG"
    exit 0
  fi
  RESULT=$(jq --arg name "$NAME" --argjson entry "$ENTRY" \
    '.mcpServers[$name] = $entry' "$CFG" 2>/dev/null \
    || jq -n --arg name "$NAME" --argjson entry "$ENTRY" '{mcpServers:{($name):$entry}}')
  if [ "$CHECK" = "1" ]; then
    echo "== would write $CFG =="
    echo "$RESULT" | jq .
    echo "== (dry-run: nothing written; Claude expands \${VAULT_API_TOKEN} at runtime) =="
    exit 0
  fi
  umask 077
  printf '%s\n' "$RESULT" > "$CFG"
  echo "installed $NAME -> $CFG (token via \${VAULT_API_TOKEN} env expansion)"
  exit 0
fi

# ---- json: generic mcpServers payload --------------------------------------
if [ "$CLIENT" = "json" ]; then
  if [ "$REMOVE" = "1" ]; then
    echo "json mode has no file to remove; drop the mcpServers entry from the target"
    exit 0
  fi
  ENTRY=$(jq -n --arg url "$URL" --arg token "${TOKEN:-<VAULT_API_TOKEN>}" \
    '{type:"http", url:$url, headers:{Authorization:("Bearer "+$token)}}')
  jq -n --arg name "$NAME" --argjson entry "$ENTRY" '{mcpServers:{($name):$entry}}'
  exit 0
fi

# ---- gh / copilot: interactive guidance -------------------------------------
if [ "$CLIENT" = "gh" ]; then
  cat <<GUIDE
gh copilot MCP is interactive-only (no config file to hand-edit).
Run:  gh copilot --help  then the interactive MCP flow, and register:
  name:    $NAME
  type:    http (streamable HTTP)
  url:     $URL
  header:  Authorization: Bearer <token from the modal-vault-secret Secret>
GUIDE
  exit 0
fi

echo "unknown --client: $CLIENT (codex|claude|json|gh)" >&2
exit 2