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
git checkout <tag>          # v1.2.4 = current release of record
uv run --project server pytest server/tests -q   # 169 must pass
```

Then re-run the lane's `deploy.sh` (scaled-to-zero posture stays:
Kyle's ruling = the live vault stays OFF until he says otherwise).
NEVER `git reset`/re-clone to "fix" the version — that wipes local
lane state and can misalign tags.

## Bootstrap + secrets (the overlay carries them)

- The app self-bootstraps on first boot from the Secret's five
  `VAULT_OB_*` keys (argv login, state-root fixed upstream; the
  writing-duo wrapper may keep precedence for its `OB_*` names).
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
