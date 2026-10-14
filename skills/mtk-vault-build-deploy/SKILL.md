---
name: mtk-vault-build-deploy
description: Use when deploying or upgrading the vault from a project - secrets gate, drift check, seed expectations, and the upgrade path.
---

# Vault: deploy and upgrade

## Deploy

```bash
mtk deploy vault
```

What runs, in order, all gate-refusing before any account write: the overlay
preflight (files + composed app name), the vendor drift check
(`vendor/vault-server/` HEAD vs provenance, clean worktree), the secrets gate
(lists missing, asks per secret), then the overlay's `deploy.sh`.

An overridden drift deploy needs the explicit `--allow-local-changes`; it is
recorded in the project state only after deploy.sh exits 0. It is never the
default.

The vault has no `seed.json` (no model store): `mtk seed vault` offers nothing.

## Verify

```bash
mtk doctor --pkg vault                 # health + secrets lines
curl -s https://<host>/health          # vault + sync state; first-call cold is normal
gh run list --repo kylebrodeur/modal-vault-server --limit 3   # CI on the pinned tag
```

## Upgrade

The upgrade unit is the upstream release tag:

```bash
mtk pull vault --tag v1.6.0     # asks before replacing the pin
mtk deploy vault
```

A tag without a GitHub release is not published; check
`gh release view <tag> --repo kylebrodeur/modal-vault-server` before pinning a
claim. Never reset or wipe a vendored copy to fix version confusion: re-vendor
from the recorded tag instead.