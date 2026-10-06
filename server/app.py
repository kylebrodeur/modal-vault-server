"""Modal App: image, volume, secret, and the single web function.

Deploy from the repo root (the Modal CLI loads this file with the repo root on
the path, which keeps stdlib `types` clear of server/types.py):

    uvx modal deploy server/app.py

Image: node:24-bookworm-slim + Python 3.12 (the `ob` headless-sync binary is an
npm package, so the base image must carry node; marksman is not needed - the v1
link graph reads markdown links directly). The server's Python runtime is
pip-installed on top, and `ob --version` runs as a build gate: a broken image
build fails loudly instead of crash-looping a container at boot.

The HTTP contract lives in web.py (importable without the Modal SDK so it can
be tested with a TestClient); this file wires it to the Modal Volume, Secret,
and the scale-to-zero web function. Boot pulls once (failures are contained by
design: last-good clone keeps serving, /health reports degraded) and serving is
fail-closed: without VAULT_API_TOKEN the build raises and nothing serves.
"""

from __future__ import annotations

import modal
from fastapi import FastAPI

from server.config import Config
from server.sync_service import SyncService
from server.web import build_app, run_boot

APP_NAME = "modal-vault-server"

SECRET_NAME = "modal-vault-secret"

VOLUME_NAME = "modal-vault"
VOLUME_VERSION = 2

# Env contract: the image exports the mount roots as VAULT_* env vars, so
# Config.load() inside the container reads exactly these paths. /state reaches
# the Volume via the image's /state -> /vault/state symlink (Modal forbids
# mounting one Volume at two roots); ob login state + watermark persist.
VAULT_DATA_DIR = "/vault"
VAULT_STATE_DIR = "/state"

# Modules the container imports at serve() time (plus this one);
# `server.*` package imports resolve because the package root ships with the image.
_SERVE_MODULE_NAMES = (
    "server.app",
    "server.config",
    "server.mcp_tools",
    "server.search_scan",
    "server.sync_service",
    "server.web",
    "server.linker",
    "server.types",
)

image = (
    modal.Image.from_registry("node:24-bookworm-slim", add_python="3.12")
    .pip_install("fastapi[standard]>=0.115", "mcp>=1.0", "pyyaml>=6")
    .run_commands(
        # /state rides the same Volume as /vault (ob login state + watermark must
        # persist): Modal forbids mounting one Volume at two roots, so /state is a
        # symlink into the mounted clone's state subdir.
        "ln -sfn /vault/state /state",
        "npm install -g obsidian-headless@0.0.14",
        "ob --version",  # build gate: fail the image build if the ob binary is missing
    )
    .env({"VAULT_DATA_DIR": "/vault", "VAULT_STATE_DIR": "/state"})
    .add_local_python_source(*_SERVE_MODULE_NAMES)
)

app = modal.App(APP_NAME, image=image)

vault_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True, version=VOLUME_VERSION)

vault_secret = modal.Secret.from_name(SECRET_NAME)

_SERVE_VOLUMES = {VAULT_DATA_DIR: vault_volume}


def _build_serving_app() -> FastAPI:
    """Config + SyncService + boot + FastAPI; the whole serve() body minus Modal.

    Fail-closed at boot: an empty/missing VAULT_API_TOKEN raises (loud, no
    serving) - the bearer gates on /mcp and /admin/* must never run with a
    blank token that would reject everything anyway. Boot sync failures are
    contained by run_boot's design (degraded /health, last-good clone serves).
    """
    cfg = Config.load()
    if not cfg.api_token:
        raise RuntimeError(
            "VAULT_API_TOKEN is empty: refusing to serve. "
            "Put the bearer token in the modal-vault-secret Secret."
        )
    sync = SyncService(cfg.data_dir, cfg.state_dir)
    run_boot(cfg, sync)
    return build_app(cfg, sync)


@app.function(
    image=image,
    scaledown_window=300,
    timeout=3600,
    max_containers=1,
    volumes=_SERVE_VOLUMES,
    secrets=[vault_secret],
)
@modal.asgi_app()
def serve() -> FastAPI:
    """The web function: FastAPI app with /health, bearer /admin/sync, MCP at /mcp."""
    return _build_serving_app()
