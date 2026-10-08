"""Test helper: writes an executable fake `ob` shell shim; tests prepend its dir to PATH.

Behavior mirrors the REAL obsidian-headless contracts (verified live 2026-10-08):
- logs one `start <argv> cwd=<cwd> xdg=<XDG_CONFIG_HOME>` line per invocation
- `login` MUST be argv-flag driven to persist: with `--email/--password` flags the
  shim records `persisted:auth_token`; with only stdin data (a legacy TTY-style
  call) it logs the stdin but persists NOTHING (the silent no-op contract)
- `whoami` is not a real command: any whoami invocation exits 127 (binary-says-no)
- `sync` rejects `--mode` (exit 64, "invalid flag") when given one; bare
  `sync --path <dir>` exits 0 and logs `pulled`
- `sync-config --mode pull-only` logs `configured:pull-only`

OB_FAKE_MODE retains the legacy failure/shaping knobs (`fail`, `slow`, `hang`).
"""

from __future__ import annotations

import os
from pathlib import Path

SHIM = r"""#!/bin/sh
log="${OB_FAKE_LOG:-/dev/null}"
printf 'start %s cwd=%s xdg=%s\n' "$*" "$PWD" "${XDG_CONFIG_HOME:-}" >> "$log"

if [ -n "${OB_FAKE_WHOAMI:-}" ] && [ "$1" = "whoami" ]; then
    exit "${OB_FAKE_WHOAMI:-0}"
fi

# login: flags persist a token; stdin-only silently does not (real-binary contract)
if [ "$1" = "login" ]; then
    stdin_data="$(cat)"
    [ -n "$stdin_data" ] && printf 'stdin:%s\n' "$stdin_data" >> "$log"
    case "$*" in
        *--email*--password*)
            state="${XDG_CONFIG_HOME:-/nonexistent}/obsidian-headless"
            mkdir -p "$state" && printf 'token-fake\n' > "$state/auth_token"
            printf 'persisted:auth_token\n' >> "$log"
            ;;
    esac
fi

if [ "$1" = "sync" ]; then
    case "$*" in
        *--mode*) printf 'ERROR: sync takes only --path/--continuous\n' >&2; exit 64 ;;
        *) printf 'pulled\n' >> "$log" ;;
    esac
fi

if [ "$1" = "sync-config" ]; then
    case "$*" in
        *--mode*pull-only*) printf 'configured:pull-only\n' >> "$log" ;;
    esac
fi

case "${OB_FAKE_MODE:-ok}" in
fail)
    i=0
    while [ "$i" -lt 27 ]; do
        printf 'ERROR-LINE-%03d-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' "$i" >&2
        i=$((i + 1))
    done
    exit 1
    ;;
slow)
    sleep "${OB_FAKE_SLEEP:-0.2}"
    printf 'end %s\n' "$*" >> "$log"
    exit 0
    ;;
hang)
    printf 'HUNG-STDERR-LINE\n' >&2
    sleep "${OB_FAKE_SLEEP:-3}"
    exit 0
    ;;
*)
    exit 0
    ;;
esac
"""


def install(bin_dir: Path) -> Path:
    """Write the shim to <bin_dir>/ob (0o755) and return the shim path (prepend .parent to PATH)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "ob"
    shim.write_text(SHIM, encoding="utf-8")
    os.chmod(shim, 0o755)
    return shim
