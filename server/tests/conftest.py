"""Shared web-test boot helper: a TestClient with run_boot already applied.

`booted_client` runs the real boot path (`run_boot`) before the app serves, mirroring
Task 8's serve() sequence, so /health, /admin/sync, and the MCP mount all see the
state dir exactly as production would leave it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import web
from server.config import Config
from server.types import SyncResult


@dataclass
class Harness:
    """Everything a web test needs: client, config paths, the app, and the sync stub."""

    client: TestClient
    app: FastAPI
    cfg: Config
    vault_dir: Path
    state_dir: Path
    sync_stub: Any  # SyncStub


class SyncStub:
    """Scriptable stand-in for SyncService: records one_shot calls, returns canned results.

    The first result consumes as a call WITHOUT counting (the harness spends it on
    run_boot); later results count. Tests assert post-boot deltas.
    """

    def __init__(self, results: list[SyncResult]) -> None:
        self._results = list(results)
        self.calls: list[int] = []
        self._booted = False

    def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if not self._booted:
            self._booted = True
            return result
        self.calls.append(timeout_seconds)
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _app(cfg: Config, sync_stub: SyncStub) -> FastAPI:
    return web.build_app(cfg, sync_stub)


@pytest.fixture
def boot_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory: boot_harness(sync_results=[SyncResult(True, ...)]) -> booted Harness.

    Always runs run_boot (like Task 8's serve()) then wraps the app in TestClient.
    """
    vault_dir = tmp_path / "vault"
    state_dir = tmp_path / "state"
    vault_dir.mkdir()
    state_dir.mkdir()

    def factory(
        sync_results: list[SyncResult] | None = None,
        token: str = "tok",
        notes: dict[str, str] | None = None,
    ) -> Harness:
        sync_results = sync_results if sync_results is not None else [SyncResult(ok=True, mode="pull-only", detail="")]
        if notes:
            for rel, text in notes.items():
                path = vault_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        cfg = Config(api_token=token, data_dir=vault_dir, state_dir=state_dir)
        sync_stub = SyncStub(sync_results)
        web.run_boot(cfg, sync_stub)  # boot before serving, as Task 8's serve() will
        app = _app(cfg, sync_stub)
        return Harness(
            client=TestClient(app),
            app=app,
            cfg=cfg,
            vault_dir=vault_dir,
            state_dir=state_dir,
            sync_stub=sync_stub,
        )

    return factory
