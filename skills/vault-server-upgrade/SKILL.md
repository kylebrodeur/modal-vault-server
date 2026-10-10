---
name: vault-server-upgrade
description: The vault lane's upgrade + deploy contract: overlay-only deploys, tag upgrades, secret/bootstrap flow, and the write-door posture. Born from the 2026-10-09 wipe incident.

Use when working on modal-vault-server, upgrading or redeploying the writing-duo vault lane, or before ANY modal command inside this checkout.
license: Apache-2.0
metadata:
  author: kylebrodeur
  family: modal-toolkit
  repo: modal-vault-server
---

# vault-server: upgrade + deploy boundary (lane agents; WRITTEN IN BLOOD)

This is the repo whose live lane a rogue deploy WIPED on 2026-10-09
(an agent deployed straight from this public checkout: same app name,
same Volume, its own sync — the lane's clone state lost). The rules:

## Never deploy from this checkout

```bash
# from the system workspace, BEFORE any modal command:
tools/guards/deploy-provenance.sh <your-overlay-dir>
```

- Refuses plain-repo deploys + app names not shaped `<slug>-vault`.
- The writing-duo lane's overlay: `writing-duo/deploys/vault-server/`
  (`deploy.json` composes the lane's app name + secret names).
- The LIVE app for a lane = that lane's composed name; a second app on
  the shared Volume = the wipe vector. `modal app list` before AND
  after any deploy touching a vault-shaped name.

## Upgrading this checkout (the ONLY correct move)

```bash
git fetch --tags
git checkout <tag>          # v1.4.0 = current release of record (adds MCP OAuth 2.1)
uv run --project server pytest server/tests -q   # 189 must pass
```

Then re-run the lane's `deploy.sh` (scaled-to-zero posture stays:
Kyle's ruling = the live vault stays OFF until he says otherwise).
NEVER `git reset`/re-clone to "fix" the version — that wipes local
lane state and can misalign tags.

**Crossing v1.3.0 is BREAKING for lanes pinned at v1.2.4.** v1.3.0
renamed every knob to `MODAL_VAULT_*` with NO legacy fallback: the
Secret must be REBUILT with the new key names (`MODAL_VAULT_API_TOKEN`
+ the five `MODAL_VAULT_OB_*`) BEFORE deploying anything v1.3.0+ — the
app is fail-closed without `MODAL_VAULT_API_TOKEN`. Rebuilding the
Secret rotates the bearer, so every MCP client must pick up the new
token. v1.4.0 (OAuth) is additive on top: `MODAL_VAULT_MCP_AUTH`
defaults to `token`, so the static surface is unchanged until a lane
opts into `both`/`oauth`.

## MCP auth (v1.4.0+)

- `MODAL_VAULT_MCP_AUTH` = `token` (default) | `oauth` | `both`;
  `MODAL_VAULT_MCP_AUTH_ISSUER` = the app origin (issuer). OAuth lets
  URL-only clients (Claude, Cursor, Gemini Spark) connect via DCR +
  PKCE; the operator approves the in-app `/consent` page once with the
  static token.
- OAuth state (client registry + token hashes) lives on the Volume at
  `/vault/state/mcp-as/`. Rotating the static token does NOT invalidate
  issued OAuth tokens; `POST /revoke` does.

## Bootstrap + secrets (the overlay carries them)

- The app self-bootstraps on first boot from the Secret's five
  `MODAL_VAULT_OB_*` keys (argv login, state-root fixed upstream). One
  vocabulary: no short `OB_*` fallback (removed so a shell export can
  never resolve to another server's value).
- Secret NAMES are public, values never written; manifests in
  `server/secrets.toml`. Rotation = `mtk secrets rotate` (invalidates
  live users — flag it).

## Hooks + provenance

- `server/hooks.py` pre-binds the vendored canonical
  (`server/libs/hooks.py`); tags: `boot.pre`, `boot.post`,
  `write.post`.
- Lanes register the boot-provenance observer
  (`tools/guards/boot-provenance.py` in the system workspace): every
  boot REPORTS `deployed_by={slug, overlay, app}`. A boot report
  showing `unslugged/none` = rogue deploy: shut it down
  (`modal app stop`) and surface it to Kyle immediately.
- Write door: pulls are safe any time; writes are posture-gated
  (pull-only default). A lane flipping sync-mode must be Kyle-asked.
