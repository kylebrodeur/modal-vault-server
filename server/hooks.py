"""Upstream's integration seam: named hooks + env-name fallbacks.

THE model for lanes that build on the vault server (writing-duo's overlay,
future agent lanes): the mechanism is now SHARED — this module re-exports
the canonical `libs.hooks` (vendored at `server/libs/hooks.py`, source:
modal-shared-libs) pre-configured with the vault's tag set, so lane code
reads the same API as the family while the contract stays canonical.

- HOOK IN, don't monkeypatch: register named functions at boot/write
  lifecycle tags from a deploy root; upstream fires them at stable points.
  Multiple registrations coexist (built-on-top); call order is
  registration order, so a lane can wrap by registering both before and
  after.
- OVERRIDE through config/env, not edits: app name, secret names, and the
  ob credential env names are remappable without touching upstream code.

Contract guarantees (canonical, in libs.hooks):
- A declared, closed tag list (TAGS below).
- Hook calls never raise into upstream behavior: a hook error is contained
  and REPORTED via `last_errors(tag)` — a broken lane hook degrades that
  lane, never the server.
- The tag list is closed and documented (TAGS below); new tags are
  upstream-versioned work, not improvised.
"""

from __future__ import annotations

from server.libs.hooks import Hooks

# The stable lifecycle tags (upstream contract; versioned like the wire).
TAG_BOOT_PRE = "boot.pre"  # before login/pull: (cfg, sync-service)
TAG_BOOT_POST = "boot.post"  # after boot: (cfg, boot_report dict)
TAG_WRITE_POST = "write.post"  # after each write/delete/revert: (write_report dict)

TAGS = (TAG_BOOT_PRE, TAG_BOOT_POST, TAG_WRITE_POST)

hooks = Hooks(TAGS, name="modal-vault-server")

# One namespace for lane code: the shared seam's surface, pre-bound.
register = hooks.register
on = hooks.on
fire = hooks.fire
last_errors = hooks.last_errors
registrations = hooks.registrations
clear = hooks.clear


def reset() -> None:
    """Test seam: clear everything."""
    hooks.clear()
