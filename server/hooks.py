"""Upstream's integration seam: named hooks + env-name fallbacks.

THE model for lanes that build on the vault server (writing-duo's overlay,
future agent lanes):

- HOOK IN, don't monkeypatch: register named functions at boot/write
  lifecycle tags from a deploy root; upstream fires them at stable points.
  Multiple registrations coexist (built-on-top); hooks may wrap by calling
  the next handler explicitly.
- OVERRIDE through config/env, not edits: app name, secret names, and the
  ob credential env names are remappable without touching upstream code.

Contract guarantees:
- Hook calls never raise into upstream behavior: a hook error is contained
  and REPORTED in the running report (`hooks_errors`) — a broken lane hook
  degrades that lane, never the server.
- Tag list is closed and documented (see TAGS below); new tags are
  upstream-versioned work, not improvised.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

# The stable lifecycle tags (upstream contract; versioned like the wire).
TAG_BOOT_PRE = "boot.pre"  # before login/pull: (cfg, sync-service)
TAG_BOOT_POST = "boot.post"  # after boot: (cfg, boot_report dict)
TAG_WRITE_POST = "write.post"  # after each write/delete/revert: (write_report dict)

REGISTRY: dict[str, list[Callable[..., Any]]] = {tag: [] for tag in (TAG_BOOT_PRE, TAG_BOOT_POST, TAG_WRITE_POST)}

_last_errors: dict[str, list[str]] = {tag: [] for tag in (TAG_BOOT_PRE, TAG_BOOT_POST, TAG_WRITE_POST)}


def register(tag: str, fn: Callable[..., Any]) -> None:
    """Add a handler for a lifecycle tag (call order: registration order)."""
    if tag not in REGISTRY:
        raise ValueError(f"unknown hook tag {tag!r}; tags: {sorted(REGISTRY)}")
    REGISTRY[tag].append(fn)


def registrations(tag: str) -> list[Callable[..., Any]]:
    return list(REGISTRY.get(tag, ()))


def fire(tag: str, *args: Any) -> list[str]:
    """Run a tag's handlers; contain + record errors; return error strings."""
    errors: list[str] = []
    for fn in REGISTRY.get(tag, ()):
        try:
            fn(*args)
        except Exception as exc:  # contained: one lane's failure never blocks the server
            errors.append(f"{getattr(fn, '__name__', 'hook')}: {type(exc).__name__}: {str(exc)[:150]}")
    _last_errors[tag] = errors
    return errors


def last_errors(tag: str) -> list[str]:
    """The latest fire() errors for a tag (for /health-style reporting)."""
    return list(_last_errors.get(tag, ()))


def reset() -> None:
    """Test seam: clear everything."""
    REGISTRY.clear()
    _last_errors.clear()
    for tag in (TAG_BOOT_PRE, TAG_BOOT_POST, TAG_WRITE_POST):
        REGISTRY[tag] = []
        _last_errors[tag] = []


@contextlib.contextmanager
def _registered(tag: str, fn: Callable[..., Any]):
    register(tag, fn)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            REGISTRY.get(tag, []).remove(fn)
