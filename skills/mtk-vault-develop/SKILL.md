---
name: mtk-vault-develop
description: Use when building on a running vault instance - the write door, sync-mode postures, MCP and admin surfaces, and what stays off.
---

# Vault: develop against a live instance

Applies once `mtk doctor --pkg vault` shows the app live (`deployed`).

## The surface (v1.5.0)

- Read surface: `vault.status`, `vault.browse`, `vault.read_batch` (ordered
  paths + child notes with metadata; recursion via `search: true`).
- The write door (deliberate, v1.1.0): `POST /admin/notes`
  (create-or-update), `/admin/notes/delete`.
- Runtime sync postures via `POST /admin/sync-mode`: `pull-only` (default) and
  more after a lane asks; a posture flip requires the operator's approval.
- Delete stays OFF in the steady state: a windowed `allow_delete` runtime flag
  (`POST /admin/allow-delete`, self-expiring, restart-safe) can arm it, and
  armed deletes still need a successful pre-delete snapshot. No snapshot, no
  delete.
- MCP: `/mcp` on the deployed host; OAuth consent page sign-in; dynamic client
  registration works with Claude, Cursor, Gemini Spark.

## Rules

- Bearer token from the `modal-vault-secret` Secret; never commit real
  `MODAL_VAULT_API_TOKEN` values.
- Data plane: real sync happens INSIDE the vault container on the Volume-backed
  workspace (`mtk vault ...` passthrough), never local sync on the operator
  machine.
- Still open upstream: semantic search (`VAULT_EMBEDDING_URL`), hybrid
  ranking, the ledger/memory-plane door. Feature work in those areas is
  upstream work, not overlay work.

## Verify

Changes to the write surface against the live app: hit the admin endpoints
with the bearer and inspect responses; `mtk doctor --pkg vault` after any
config change.