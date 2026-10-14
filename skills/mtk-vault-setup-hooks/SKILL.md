---
name: mtk-vault-setup-hooks
description: Use when wiring lifecycle hooks on a vendored vault copy - registering observers against the server's hooks seam without touching vendored code.
---

# Vault: setup hooks and provenance observers

The vault carries a lifecycle hook seam: one shared instance named
`modal-vault-server`, closed tag set `boot.pre`, `boot.post`, `write.post`.
Register without editing vendored code - the seam lives next to the real stage
boundaries, and handler errors are contained and reported
(`last_errors(tag)`), never the server.

## Register an observer

In the overlay (a small module the overlay adds to PYTHONPATH), never in
`vendor/`. Import the seam exactly as the README shows:

```python
from server.hooks import hooks


@hooks.on("boot.post")
def observe(cfg, boot_report):
    print(boot_report)
```

Tags and payloads: `boot.pre` before login/pull handler `(cfg, sync-service)`;
`boot.post` after boot, handler `(cfg, boot_report dict)`; `write.post` after
each write/delete/revert, handler `(write_report dict)`. Handler errors are
contained and reported (`last_errors(tag)`), never the server.

## Boot provenance reporting

Lane deploys register a `boot.post` observer (the `boot-provenance.py`
pattern) that reports `deployed_by`. A boot reporting
`deployed_by: unslugged/none` means a rogue deploy: shut it down
(`mtk shutdown` / `modal app stop`) before anything else.

## Where

- Overlay module: `deploys/vault-server/` (your project owns it).
- The seam contract: the vendored repo's README (lifecycle hooks section).
- Verify a registration: boot once (or `mtk doctor --pkg vault`), then read
  `last_errors(tag)` in the observer output.