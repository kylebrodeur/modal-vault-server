---
name: mtk-vault-init-config
description: Use when a project adds the vault package - mtk init, the overlay pair (deploy.json, deploy.sh), and what to check before the first deploy.
---

# Vault: init and config for a project

## Init

```bash
mtk init vault-lab        # refuses an id that would collide with a live Modal app
cd vault-lab
mtk pull vault --tag v1.5.0   # or the current release tag
```

`mtk pull` vendors the exact release tag into `vendor/vault-server/` and writes
its provenance. The copy is read-only; drift refuses a deploy.

## The overlay

`deploys/vault-server/deploy.json` names the app `<project>-vault`:

```json
{ "pkg": "vault", "app_name": "vault-lab-vault" }
```

`deploys/vault-server/deploy.sh` is the deploy entry point. Deploy goes through
`mtk deploy vault`, never `modal deploy` inside a checkout or the vendored copy.

## Before the first deploy

- The secrets manifest (`server/secrets.toml` in the vendored copy) declares
  `modal-vault-secret` (key `MODAL_VAULT_API_TOKEN`, generated). `mtk deploy`
  lists missing secrets and asks before creating each.
- First-boot login is optional: `MODAL_VAULT_OB_*` keys in the secret enable
  self-provisioning when the state dir is empty; without them the vault boots
  degraded and says so honestly.

## Verify

```bash
mtk doctor --pkg vault
curl -s https://<host>/health    # 503 or a cold start on the first call is normal
```