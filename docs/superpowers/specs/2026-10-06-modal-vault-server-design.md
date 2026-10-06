# modal-vault-server — design

Date: 2026-10-06
Status: approved in chat, awaiting implementation
Sibling of: modal-embedding-server, modal-inference-server, modal-vision-server, modal-finetune-server, modal-toolkit, embed-eval-on-your-vault

## Purpose

A hosted vault + MCP memory plane. One Modal CPU app that:

1. keeps a server-side clone of a user's Obsidian vault current via Obsidian Headless Sync,
2. maintains a searchable projection of that vault (LanceDB vectors + note-link graph) on a Modal Volume,
3. exposes the vault to any MCP client (Pi, Claude Code, any MCP-speaking agent) as memory tools.

The user's words from the design conversation: users "can sync and host their obsidian vaults where agents/agent systems can live and work."

## Decisions from the design conversation

| Question | Decision |
|---|---|
| Consumer model | MCP-first data plane. Agents connect TO it; no agent runtimes deployed in slice 1. Server-side agent runtimes are a possible later door, not built. |
| Vault ingestion | Obsidian Headless Sync (`ob` CLI, server-side clone). Proven live on the pi-vault-mind `feat/headless-graph-rebuild` branch. Desktop flow unchanged; Obsidian Sync is the transport. |
| MCP read/write | Read first (slice 1); write-back second (slice 2). |
| Embeddings + ledgers | Out of scope for this repo. Query-time embedding goes through an integration hook pointing at an OpenAI-compatible `/embed`-style endpoint (modal-embedding-server shape). JSONL ledger/memory plane stays in pi-vault-mind; a later slice may add integration hooks. |
| Repo | New public family repo `modal-vault-server`, family conventions (uv/pytest/ruff CI, family README section, byline). |

## Architecture

Single Modal app, scale-to-zero, sync-on-boot ("Approach A" from the discussion):

```
Modal Volume (v2):
  /vault   — the ob Headless Sync clone (pull-only)
  /index   — LanceDB vectors + graph tables
  /state   — operation state, ob login state, watermarks

cold boot:
  1. load op-state from /state (ob login persisted; 1Password secret only at first bootstrap)
  2. ob sync (one-shot, pull-only) → vault clone current
  3. indexer: notes changed since watermark → embed via hook → upsert LanceDB
  4. linker: rebuild note-link graph for changed notes
  5. /health green → MCP serves

MCP request: bearer token → tool handler → read clone/index → respond
```

Slice 2 (write-back): `vault.write` edits the clone, then `ob push`; Obsidian Sync
delivers the edit to the user's devices. Pull-only in slice 1 keeps the write path
closed while the read path proves out.

Rejected alternatives (from the discussion):
- **B: split persistent `syncd` + scale-to-zero MCP.** Cleaner always-fresh clone,
  but 24/7 warm container cost and two deploys. Approach A is structured so this is
  an additive slice later (the sync loop is its own module).
- **C: monolithic always-on host.** Pre-spends future complexity at worst cost posture.

## Repo layout

```
modal-vault-server/
  docs/superpowers/specs/    (this doc + plans)
  server/
    app.py          # Modal App: image, secrets, volume, functions
    web.py          # FastAPI: MCP streamable-HTTP mount at /mcp + /admin/* + /health
    mcp_tools.py    # MCP tool handlers (pure, no transport)
    sync_service.py # ob CLI orchestration: login/bootstrap/one-shot/pull
    indexer.py      # vault → whole-note chunks → embedding hook → LanceDB (watermarked)
    linker.py       # note-link graph from markdown links (no marksman in v1)
    store.py        # LanceDB-on-Volume; watermarks; amortized commits
    config.py       # env knobs, VAULT_* prefix
    tests/          # pytest: ob shim, in-process MCP client, temp volumes
  pyproject.toml, uv.lock
  .github/workflows/ci.yml   # same shape as siblings: uv sync, pytest, ruff
  README.md, LICENSE, SECURITY.md, CONTRIBUTING.md, .github/FUNDING.yml
  .github/ISSUE_TEMPLATE/, dependabot.yml
```

All Python. No TS worker, no node runtime. `ob` is a subprocess binary; the v1 link
graph reads links directly from markdown instead of using marksman (family stays
single-runtime; marksman can return later if link resolution needs it).

## MCP surface (slice 1: read)

- `vault.search(query, mode, k, collections?)` — modes `semantic` / `fts` /
  `hybrid` over whole-note embeddings; `graph` walks note-link relations (semantic
  edges arrive with a later ledger slice). Query embedding goes through the hook:
  `VAULT_EMBED_URL` + `VAULT_EMBED_API_TOKEN`, modal-embedding-server `/embed`
  contract, `task=query`. Hook unset → semantic/hybrid degrade to `fts` with an
  honest note in the result payload. Never crashes; never silently pretends.
- `vault.read(path)` — note text + parsed frontmatter.
- `vault.list(prefix?, tag?, collection?)` — inventory with metadata.
- `vault.query_graph(seed, depth, kind)` — note-link edges in v1.
- `vault.status()` — sync phase / last-sync-at, index watermark, hook reachability.

Admin (HTTP, bearer, not MCP): `POST /admin/reindex` — full watermark reset +
re-embed through the hook. Batched and amortized, phase-aware progress with
denominators (the lesson carried from the stalled pi-vault-mind path: few immutable
passes, no commit-per-row, phase + processed/planned reporting).

## Explicit non-goals (slice 1)

- No JSONL ledger plane; no `collections/*.jsonl` writes.
- No MCP-triggered reindex or reembed job API.
- No chunking: whole-note embedding only (decided by the Q4 A/B evidence: recall@1 0.709 whole-note vs 0.653 chunked, 18x index bloat).
- No Obsidian Sync mirror/bidirectional config in v1; pull-only.
- No multi-vault support.
- No agent runtimes, sandboxes, or session registries.
- Each is a later slice at a clean seam; the code structure must not prejudice these doors.

## Failure handling + ops posture

- Boot sync failure → serve last-good clone + index from the Volume, `/health`
  reports degraded, MCP still answers reads. Fail-closed serving, never crash.
- `ob` lock conflicts: single-function slot gate (family pattern).
- All Volume commits batched/amortized.
- Auth: bearer `VAULT_API_TOKEN` on `/mcp` + `/admin/*`. One deployment = one user (v1).
- Secrets: `modal-vault-secret` carries `VAULT_API_TOKEN`; first-bootstrap-only
  1Password SDK path (proven phase model: credentials → auth → sync) for `ob` login.
  The 1Password path is used at provisioning, then the login state persists on the
  Volume and the secret is not needed at steady state.

## Testing strategy

pytest, family conventions:

- fake `ob` binary shim (pattern proven in the paused branch's tests) for sync_service.
- store/watermark tests on a temp LanceDB dir.
- MCP tools exercised through the official `mcp` SDK's in-process client against
  `web.py`'s streamable-HTTP app (real transport code path in tests).
- indexer: watermark/dedupe/changed-note behavior on a fixture vault.
- linker: fixture vault with wikilinks and relative links.
- CI: `uv sync --group dev`, `pytest`, `ruff` — the exact sibling workflow shape.

## Provenance

The durable-operation store design and worker phase model (credentials → auth →
sync → index) are ported as Python from `feat/headless-graph-rebuild`, with private
references scrubbed during extraction. No content is copied verbatim from the
private repo.

## Family integration (on release)

- README "Part of the Modal Toolkit" family section gains `modal-vault-server`
  here, and the sibling six repos' family sections gain this repo's bullet.
- Bullet wording: "Hosted vault + MCP memory plane: server-side Obsidian clone
  via Headless Sync with a searchable projection agents reach over MCP."
- Toolkit `mtk` can grow a `vault` verb later (doctor/warm/shape), not in v1 scope.