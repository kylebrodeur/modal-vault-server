"""Task 8 tests: app.py Modal wiring (image, volume, secret, serve boot discipline).

No deploy, no network: module wiring is asserted at import (the Modal objects are
real) and the serve() build path is exercised through ``_build_serving_app`` with
VAULT_* env + the fake ``ob`` shim on PATH, mirroring web.py's boot-before-serve
harness.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType

import modal
import pytest
from fastapi import FastAPI
from starlette.routing import Mount, Route

from server import app as app_module
from server.app import _build_serving_app
from server.tests.fake_ob import install

_APP_SOURCE = Path(app_module.__file__).read_text(encoding="utf-8")

# Modules the deployed container imports at serve() time (image build completeness).
_SERVE_IMPORTS = ("config", "mcp_tools", "search_scan", "sync_service", "web")


class TestAppWiring:
    """Import-level wiring: the Modal app object and its infra attachments."""

    def test_app_name(self) -> None:
        assert app_module.APP_NAME == "modal-vault-server"
        assert app_module.app.name == "modal-vault-server"

    def test_image_base_is_node_slim_with_python(self) -> None:
        assert 'from_registry("node:24-bookworm-slim", add_python="3.12")' in _APP_SOURCE

    def test_ob_installed_and_version_checked(self) -> None:
        assert "npm install -g obsidian-headless@0.0.14" in _APP_SOURCE
        assert "ob --version" in _APP_SOURCE  # image build fails if the binary is missing

    def test_no_marksman(self) -> None:
        # Amendment: marksman is not installed; the v1 link graph reads markdown directly.
        assert "marksman-linux" not in _APP_SOURCE
        assert "MARKSMAN" not in _APP_SOURCE

    def test_pip_install_covers_runtime_deps(self) -> None:
        assert '"fastapi[standard]' in _APP_SOURCE
        assert '"mcp' in _APP_SOURCE
        assert '"pyyaml' in _APP_SOURCE

    def test_local_python_source_lists_serve_imports(self) -> None:
        for module in _SERVE_IMPORTS:
            assert f'"server.{module}"' in _APP_SOURCE

    def test_serve_module_names_cover_every_server_module(self) -> None:
        # Image-completeness guard: the container crash-looped at v1.2.2 because
        # `_SERVE_MODULE_NAMES` omitted the hooks seam modules while every test
        # (and CI) still passed. Derive the module set from the FILESYSTEM so a
        # future slice that adds a server/*.py file cannot ship the same class of
        # unbootable image.
        server_dir = Path(app_module.__file__).resolve().parent
        declared = set(app_module._SERVE_MODULE_NAMES)
        on_disk: set[str] = set()
        for path in sorted(server_dir.rglob("*.py")):
            rel = path.relative_to(server_dir)
            parts = rel.with_suffix("").parts
            # Skip test scaffolding and any non-package dir (.venv, __pycache__,
            # hidden caches): only real server.* modules can enter the image.
            if any(part.startswith(".") or part in ("tests", "__pycache__") for part in parts):
                continue
            if parts[-1] == "__init__":
                continue
            on_disk.add("server." + ".".join(parts))
        missing = sorted(on_disk - declared)
        assert not missing, f"modules imported by the container but absent from _SERVE_MODULE_NAMES: {missing}"

    def test_volume_name_version_two(self) -> None:
        assert app_module.VOLUME_NAME == "modal-vault"
        assert app_module.VOLUME_VERSION == 2
        assert "version=VOLUME_VERSION" in _APP_SOURCE or "version=2" in _APP_SOURCE

    def test_volume_mounts_match_env(self) -> None:
        assert app_module.VAULT_DATA_DIR == "/vault"
        assert app_module.VAULT_STATE_DIR == "/state"
        assert 'VAULT_DATA_DIR"] = "/vault"' in _APP_SOURCE or '.env({"VAULT_DATA_DIR": "/vault"' in _APP_SOURCE
        assert 'VAULT_STATE_DIR"] = "/state"' in _APP_SOURCE or '"VAULT_STATE_DIR": "/state"' in _APP_SOURCE

    def test_secret_name(self) -> None:
        assert app_module.SECRET_NAME == "modal-vault-secret"
        assert "Secret.from_name(SECRET_NAME)" in _APP_SOURCE

    def test_function_knobs(self) -> None:
        assert "scaledown_window=300" in _APP_SOURCE
        assert "timeout=3600" in _APP_SOURCE
        assert "max_containers=1" in _APP_SOURCE

    def test_serve_function_mounts_our_volume_and_secret(self) -> None:
        # Object-level: the serve Function is wired to the Volume/Secret objects this module creates.
        # One mount point (/vault); /state reaches the Volume through the image symlink.
        assert {app_module.VAULT_DATA_DIR: app_module.vault_volume} == app_module._SERVE_VOLUMES
        assert app_module.vault_volume.name == app_module.VOLUME_NAME
        assert app_module.vault_secret.name == app_module.SECRET_NAME
        assert app_module.app.name == app_module.APP_NAME

    def test_state_dir_persists_via_symlink(self) -> None:
        # /state must land on the Volume (ob login state + watermark survive cold boots):
        # Modal forbids one Volume at two mount points, so the image symlinks /state -> /vault/state.
        assert "ln -sfn /vault/state /state" in _APP_SOURCE


def _env_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_dir = tmp_path / "vault"
    state_dir = tmp_path / "state"
    vault_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setenv("VAULT_API_TOKEN", "tok")
    monkeypatch.setenv("VAULT_DATA_DIR", str(vault_dir))
    monkeypatch.setenv("VAULT_STATE_DIR", str(state_dir))


class TestServeBuildPath:
    """serve()'s underlying build path: fail-closed token gate, boot, then build_app."""

    def test_missing_token_raises_loudly(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("VAULT_API_TOKEN", raising=False)
        monkeypatch.setenv("VAULT_DATA_DIR", str(tmp_path / "vault"))
        monkeypatch.setenv("VAULT_STATE_DIR", str(tmp_path / "state"))
        with pytest.raises(RuntimeError, match="VAULT_API_TOKEN"):
            _build_serving_app()

    def test_empty_token_raises_loudly(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("VAULT_API_TOKEN", "")
        monkeypatch.setenv("VAULT_DATA_DIR", str(tmp_path / "vault"))
        monkeypatch.setenv("VAULT_STATE_DIR", str(tmp_path / "state"))
        with pytest.raises(RuntimeError, match="VAULT_API_TOKEN"):
            _build_serving_app()

    def test_with_token_returns_fastapi_with_mcp_mount(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        shim = install(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("OB_FAKE_LOG", str(tmp_path / "ob.log"))
        _env_with_token(tmp_path, monkeypatch)

        application = _build_serving_app()

        assert isinstance(application, FastAPI)
        routes = [type(route) for route in application.routes]
        assert Mount in routes and Route in routes  # /mcp double route from web.build_app
        assert any(getattr(route, "path", None) == "/health" for route in application.routes)

    def test_boot_sync_failure_is_contained_not_fatal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        shim = install(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("OB_FAKE_LOG", str(tmp_path / "ob.log"))
        monkeypatch.setenv("OB_FAKE_MODE", "fail")
        _env_with_token(tmp_path, monkeypatch)

        application = _build_serving_app()  # degraded, still serves

        assert isinstance(application, FastAPI)

    def test_serve_is_modal_web_function(self) -> None:
        # serve() is a Modal web Function (not callable locally); its raw function
        # returns _build_serving_app()'s FastAPI. The build path itself is covered
        # by test_with_token_returns_fastapi_with_mcp_mount.
        assert isinstance(app_module.serve, modal.Function)
        assert app_module.serve.info.args[0]._is_web_endpoint() is True
        raw = app_module.serve.info.args[0]._raw_f_
        assert raw.__name__ == "serve"


def test_module_exports() -> None:
    assert isinstance(app_module, ModuleType)
    assert hasattr(app_module, "serve")
