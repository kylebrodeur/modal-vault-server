"""Task 7 tests: web.py surface — health, bearer admin sync, MCP mount e2e, run_boot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import ClientSession

from server import mcp_tools, web
from server.config import Config
from server.types import SyncResult

BOOT_NOTES = {
    "Note One.md": "---\ntags: a\n---\nbody alpha\n",
    "Projects/Roadmap.md": "plain roadmap body alpha\n",
}


@pytest.fixture
def booted(boot_harness):
    """Harness with a successful boot and two notes in the clone."""
    return boot_harness(sync_results=[SyncResult(ok=True, mode="pull-only", detail="")], notes=BOOT_NOTES)


class TestHealth:
    def test_ok_after_good_boot(self, booted) -> None:
        booted.client.__enter__()
        try:
            body = booted.client.get("/health").json()
            assert body["status"] == "ok"
            assert body["vault"] is True
            assert body["sync"]["mode"] == "pull-only"
            assert body["sync"]["ok"] is True
            assert isinstance(body["sync"]["last_sync_at"], str)
        finally:
            booted.client.__exit__(None, None, None)

    def test_degraded_when_vault_dir_missing(self, boot_harness) -> None:
        harness = boot_harness(sync_results=[SyncResult(ok=True, mode="pull-only", detail="")])
        harness.vault_dir.rmdir()
        harness.client.__enter__()
        try:
            body = harness.client.get("/health").json()
        finally:
            harness.client.__exit__(None, None, None)
        assert body["status"] == "degraded"
        assert body["vault"] is False

    def test_degraded_when_last_sync_failed(self, boot_harness) -> None:
        harness = boot_harness(sync_results=[SyncResult(ok=False, mode="pull-only", detail="boom")])
        harness.client.__enter__()
        try:
            body = harness.client.get("/health").json()
        finally:
            harness.client.__exit__(None, None, None)
        assert body["status"] == "degraded"
        assert body["vault"] is True

    def test_degraded_when_watermark_corrupt(self, boot_harness) -> None:
        harness = boot_harness(sync_results=[SyncResult(ok=True, mode="pull-only", detail="")])
        harness.client.__enter__()
        try:
            (harness.state_dir / "last_sync.json").write_text("not-json", encoding="utf-8")
            body = harness.client.get("/health").json()
        finally:
            harness.client.__exit__(None, None, None)
        assert body["status"] == "degraded"
        assert body["sync"]["ok"] is False

    def test_health_never_raises_on_state_dir_removed(self, booted) -> None:
        booted.client.__enter__()
        try:
            (booted.state_dir / "last_sync.json").unlink()
            body = booted.client.get("/health").json()
        finally:
            booted.client.__exit__(None, None, None)
        assert body["sync"]["ok"] is False and body["sync"]["last_sync_at"] is None


class TestAdminSync:
    def test_missing_token_401(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post("/admin/sync")
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 401
        assert booted.sync_stub.call_count == 0

    def test_wrong_token_401(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post("/admin/sync", headers={"Authorization": "Bearer wrong"})
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 401
        assert booted.sync_stub.call_count == 0

    def test_right_token_runs_one_shot_and_returns_sync_result(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post("/admin/sync", headers={"Authorization": "Bearer tok"})
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 200
        assert reply.json() == {"ok": True, "mode": "pull-only", "detail": ""}
        assert booted.sync_stub.call_count == 1
        assert booted.sync_stub.calls == [1800]  # cfg default

    def test_timeout_knob_reaches_one_shot(self, tmp_path: Path) -> None:
        """Config's sync_timeout arrives as one_shot's timeout arg (boot + admin both thread it)."""

        class RecordingSync:
            def __init__(self) -> None:
                self.seen: list[int] = []

            @staticmethod
            def is_logged_in() -> bool:
                return True  # state present: no boot login

            def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
                self.seen.append(timeout_seconds)
                return SyncResult(ok=True, mode="pull-only", detail="")

        vault_dir = tmp_path / "vault"
        state_dir = tmp_path / "state"
        vault_dir.mkdir()
        state_dir.mkdir()
        cfg = Config(api_token="tok", data_dir=vault_dir, state_dir=state_dir, sync_timeout=99)
        assert cfg.sync_timeout == 99
        sync_stub = RecordingSync()
        web.run_boot(cfg, sync_stub)
        assert sync_stub.seen == [99]  # boot threads the knob
        app = web.build_app(cfg, sync_stub)
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            reply = client.post("/admin/sync", headers={"Authorization": "Bearer tok"})
        assert reply.status_code == 200
        assert sync_stub.seen == [99, 99]  # boot + admin call, both with the knob

    def test_failure_result_is_200_with_ok_false(self, boot_harness) -> None:
        harness = boot_harness(
            sync_results=[
                SyncResult(ok=True, mode="pull-only", detail=""),  # boot
                SyncResult(ok=False, mode="pull-only", detail="pull blew up"),  # admin call
            ]
        )
        harness.client.__enter__()
        try:
            reply = harness.client.post("/admin/sync", headers={"Authorization": "Bearer tok"})
        finally:
            harness.client.__exit__(None, None, None)
        assert reply.status_code == 200
        assert reply.json() == {"ok": False, "mode": "pull-only", "detail": "pull blew up"}


class TestMcpGate:
    def test_missing_token_401(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 401

    def test_wrong_token_401(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer wrong"}
            )
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 401

    def test_right_token_reaches_protocol(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                },
                headers={"Authorization": "Bearer tok", "Accept": "application/json, text/event-stream"},
            )
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 200
        assert reply.json()["result"]["serverInfo"]["name"] == "modal-vault-server"

    def test_gate_covers_subpaths(self, booted) -> None:
        booted.client.__enter__()
        try:
            reply = booted.client.post("/mcp/deeper", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        finally:
            booted.client.__exit__(None, None, None)
        assert reply.status_code == 401


class TestMcpE2E:
    """Full SDK async client through the mounted ASGI app (transport-attached, in-process)."""

    @pytest.fixture
    def sdk_client(self, booted):
        import httpx2
        from httpx2 import ASGITransport
        from mcp.client.streamable_http import streamable_http_client

        booted.client.__enter__()  # TestClient boots the session manager lifespan
        http_client = httpx2.AsyncClient(
            transport=ASGITransport(app=booted.app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer tok"},
        )
        return streamable_http_client(
            "http://testserver/mcp", http_client=http_client, terminate_on_close=False
        ), ClientSession

    async def test_list_tools_and_status_round_trip(self, booted, sdk_client) -> None:
        streamable, session_cls = sdk_client
        async with streamable as (read, write), session_cls(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert sorted(t.name for t in tools.tools) == sorted(t["name"] for t in mcp_tools.VaultTools.TOOLS)
            res = await session.call_tool("vault.status", {})
            structured = res.structured_content
            assert structured["sync"]["mode"] == "pull-only"
            assert structured["sync"]["ok"] is True
            assert structured["notes"] == 2

    async def test_real_tool_call_read_note(self, booted, sdk_client) -> None:
        streamable, session_cls = sdk_client
        async with streamable as (read, write), session_cls(read, write) as session:
            await session.initialize()
            res = await session.call_tool("vault.read", {"path": "Note One.md"})
            body = res.structured_content
            assert body["exists"] is True
            assert body["text"].strip() == "---\ntags: a\n---\nbody alpha"
        booted.client.__exit__(None, None, None)

    async def test_keyword_search_over_wire(self, booted, sdk_client) -> None:
        streamable, session_cls = sdk_client
        async with streamable as (read, write), session_cls(read, write) as session:
            await session.initialize()
            res = await session.call_tool("vault.search", {"query": "alpha"})
            ids = [r["id"] for r in res.structured_content["results"]]
            assert "Note One.md" in ids and "Projects/Roadmap.md" in ids
        booted.client.__exit__(None, None, None)


class TestRunBoot:
    def _cfg(self, tmp_path: Path, with_notes: bool = True) -> tuple[Config, Path, Path]:
        vault_dir = tmp_path / "vault"
        state_dir = tmp_path / "state"
        vault_dir.mkdir(exist_ok=True)
        state_dir.mkdir(exist_ok=True)
        if with_notes:
            (vault_dir / "A.md").write_text("alpha body\n", encoding="utf-8")
        return Config(api_token="t", data_dir=vault_dir, state_dir=state_dir), vault_dir, state_dir

    def test_success_writes_watermark_and_returns_clean(self, tmp_path) -> None:
        class GoodSync:
            @staticmethod
            def is_logged_in() -> bool:
                return True  # state present: no boot login

            def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
                return SyncResult(ok=True, mode="pull-only", detail="")

        cfg, _vault_dir, state_dir = self._cfg(tmp_path)
        out = web.run_boot(cfg, GoodSync())
        assert out["sync"].ok is True
        assert out["degraded"] is False
        record = json.loads((state_dir / "last_sync.json").read_text(encoding="utf-8"))
        assert record == {"mode": "pull-only", "ok": True, "last_sync_at": record["last_sync_at"]}
        assert isinstance(record["last_sync_at"], str) and record["last_sync_at"]

    def test_failure_leaves_clone_untouched_and_degraded(self, tmp_path) -> None:
        class BadSync:
            @staticmethod
            def is_logged_in() -> bool:
                return True

            def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
                return SyncResult(ok=False, mode="pull-only", detail="sync failed hard")

        cfg, vault_dir, state_dir = self._cfg(tmp_path)
        before = sorted((p, p.stat().st_mtime) for p in vault_dir.rglob("*"))
        out = web.run_boot(cfg, BadSync())
        assert out["sync"].ok is False
        assert out["degraded"] is True
        assert out["sync"].detail == "sync failed hard"
        assert not (state_dir / "last_sync.json").exists()
        after = sorted((p, p.stat().st_mtime) for p in vault_dir.rglob("*"))
        assert before == after  # clone untouched (last-good)

    def test_never_raises_when_sync_impl_raises(self, tmp_path) -> None:
        class ExplodingSync:
            @staticmethod
            def is_logged_in() -> bool:
                return True

            def one_shot(self, timeout_seconds: int = 1800) -> SyncResult:
                raise RuntimeError("ob exploded")

        cfg, _vault_dir, _state_dir = self._cfg(tmp_path)
        out = web.run_boot(cfg, ExplodingSync())  # must not raise
        assert out["degraded"] is True
        assert out["sync"].ok is False
        assert "exploded" in out["sync"].detail


class TestMcpServerBuild:
    def test_handlers_registered_and_delegate(self, tmp_path) -> None:
        from server import search_scan as scan

        vault_dir = tmp_path / "v"
        vault_dir.mkdir()
        cfg = Config(api_token="t", data_dir=vault_dir, state_dir=tmp_path / "s")
        tools = mcp_tools.VaultTools(cfg=cfg, search=scan, sync=None)

        async def direct():
            server_obj = web.build_mcp_server(cfg, tools)
            entry = server_obj.get_request_handler("tools/list")
            assert entry is not None, "tools/list handler must be registered"
            listing = await entry.handler(None, None)
            assert sorted(t.name for t in listing.tools) == sorted(t["name"] for t in tools.TOOLS)
            call_entry = server_obj.get_request_handler("tools/call")

            class Params:
                def __init__(self) -> None:
                    self.name = "vault.list"
                    self.arguments: dict[str, Any] = {}

            reply = await call_entry.handler(None, Params())
            assert reply.structured_content["count"] == 0

        import asyncio

        asyncio.run(direct())
