"""Test helper: writes an executable fake `ob` shell shim; tests prepend its dir to PATH.

The shim appends one line per invocation to $OB_FAKE_LOG (`start <argv> cwd=<cwd>`),
echoes `login`'s stdin into the log under a `stdin:` line, and takes its behavior from
OB_FAKE_MODE: `ok` (default, exit 0), `fail` (~1.5KB on stderr, exit 1), `slow`
(sleep OB_FAKE_SLEEP then a trailing `end <argv>` line, exit 0), `hang` (stderr note,
then a long sleep so `one_shot(timeout_seconds=...)` exercises the timeout path).
"""

from __future__ import annotations

import os
from pathlib import Path

SHIM = r"""#!/bin/sh
log="${OB_FAKE_LOG:-/dev/null}"
printf 'start %s cwd=%s\n' "$*" "$PWD" >> "$log"
if [ "$1" = "login" ]; then
    stdin_data="$(cat)"
    printf 'stdin:%s\n' "$stdin_data" >> "$log"
fi
if [ "$1" = "whoami" ] && [ -n "${OB_FAKE_WHOAMI:-}" ]; then
    # OB_FAKE_WHOAMI=1 -> exit 1 (not logged in); "0" -> exit 0 (default keeps exit 0).
    exit "${OB_FAKE_WHOAMI:-0}"
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
