# modal-vault-server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `modal-vault-server` — an Obsidian vault host on Modal with an MCP memory plane for agents (Headless Sync clone + LanceDB projection + MCP read tools).

**Architecture:** Single scale-to-zero Modal CPU app: `ob` pull-sync on boot → watermark-based whole-note embedding of changed notes through an HTTP embedding hook → LanceDB + note-link graph on a Volume → MCP streamable-HTTP at `/mcp` plus `/health` and bearer-gated `/admin/reindex`.

**Tech Stack:** Python 3.11+, uv, pytest, ruff, FastAPI, official `mcp` SDK (streamable HTTP), LanceDB, PyArrow, httpx, Modal; `ob` (npm `obsidian-headless@0.0.14`) as an image-installed subprocess binary.

**Spec:** `docs/superpowers/specs/2026-10-06-modal-vault-server-design.md`

## Global Constraints

- Python 3.11+ only; no TypeScript/node runtime code in the repo. `ob` is installed inside the Modal image as an npm global binary and invoked as a subprocess.
- Every env knob uses the `VAULT_` prefix (`MODAL_` platform vars pass through only in `app.py`).
- Whole-note embedding only — no chunking (spec: Q4 A/B decided).
- No third-party SaaS embedding vendors: the embedding hook must be a self-hosted OpenAI-compatible `/embed` endpoint (modal-embedding-server shape).
- Bearer auth on `/mcp` and `/admin/*` via `VAULT_API_TOKEN`.
- Slice-1 sync is pull-only; no write-back.
- Amortized commits: one LanceDB commit per index pass, never per row.
- Fail-closed serving: sync failure at boot must leave `/mcp` and `/health` serving last-good state with degraded status.
- **Parallel-execution rule:** implementers write files and run tests but DO NOT run `git commit` — the controller commits after review. (Ruling recorded in the SDD ledger: parallel implementers in one checkout cannot race `index.lock`.)
- Commit messages: Conventional Commits (`feat:`, `test:`, `chore:` …), imperative, no em dashes.
- Test filenames: `server/tests/test_<module>.py`; all tests deterministic, no network (use fakes/MockTransport), no Modal deploy in CI.

## Shared Interfaces (defined here — every task consumes these exactly)

```python
# server/types.py (Task 1 creates it; DO NOT rename anything)
@dataclass(frozen=True) class NoteRow:      # one LanceDB row
    id: str            # vault-relative path, e.g. "projects/idea.md"
    vector: list[float]
    text: str          # full raw note text (whole note)
    frontmatter: dict  # parsed YAML frontmatter ({} when absent)
    sha256: str
    embedded_at: str   # ISO-8601 UTC

@dataclass(frozen=True) class EdgeRow:
    src: str           # vault-relative path
    dst: str           # vault-relative path (resolved)
    kind: str          # "note-link" in slice 1
    raw: str           # raw link text as written

@dataclass class IndexResult:
    notes_embedded: int = 0
    notes_removed: int = 0
    edges_upserted: int = 0
    skipped_reason: str | None = None

@dataclass class SyncResult:
    ok: bool
    mode: str          # "pull-only"
    detail: str        # "" on success, stderr excerpt on failure
```

```python
# server/store.py (Task 2)
class VectorStore:
    def __init__(self, root: Path) -> None
    def upsert_notes(self, rows: list[NoteRow]) -> int      # buffered
    def delete_note(self, note_id: str) -> None             # buffered
    def commit(self) -> None                                # single LanceDB commit
    def search_semantic(self, vector: list[float], k: int = 8) -> list[dict]
    def search_fts(self, query: str, k: int = 8) -> list[dict]
    def search_hybrid(self, vector: list[float], query: str, k: int = 8) -> list[dict]
    def counts(self) -> dict                                # {"notes": N, ...}

class GraphStore:
    def __init__(self, root: Path) -> None
    def upsert_edges(self, edges: list[EdgeRow], src_shas: dict[str, str]) -> int  # buffered
    def reset_edges(self) -> None
    def commit(self) -> None
    def neighbors(self, seed: str, depth: int = 1, kind: str = "note-link") -> list[dict]
```

```python
# server/sync_service.py (Task 3)
class SyncService:
    def __init__(self, workspace_dir: Path, state_dir: Path, ob_bin: str = "ob") -> None
    def is_logged_in(self) -> bool                          # `ob whoami`
    def bootstrap(self, email: str, password: str) -> None  # `ob login` (provisioning only)
    def one_shot(self, timeout_seconds: int = 1800) -> SyncResult   # `ob sync --mode pull-only`
```

```python
# server/indexer.py (Task 5)
async def embed_texts(cfg: Config, texts: list[str], task: str) -> list[list[float]] | None
    # None when hook unset; raises HookError when set but failing (caller decides degrade/crash)
async def sync_once(cfg: Config, store: VectorStore, graph: GraphStore) -> IndexResult
```

```python
# server/mcp_tools.py (Task 6)
class VaultTools:
    def __init__(self, cfg: Config, store: VectorStore, graph: GraphStore, sync: SyncService) -> None
    async def call(self, name: str, arguments: dict) -> dict
    TOOLS: list[dict]   # MCP tool definitions (name, description, inputSchema)
```

```python
# server/web.py (Task 7)
def build_app(cfg: Config) -> FastAPI    # mounts MCP at /mcp, /health, /admin/reindex
def build_mcp_server(cfg: Config, tools: VaultTools) -> object  # official SDK server instance
```

```python
# server/linker.py (Task 4)
def parse_links(text: str) -> list[str]                  # raw targets as written
def build_edge_set(vault_dir: Path, note_paths: list[str]) -> list[EdgeRow]
```

---

### Task 1: Repo scaffold + config + shared types

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.github/workflows/ci.yml`, `.github/FUNDING.yml`, `.github/ISSUE_TEMPLATE/bug_report.md`, `.github/ISSUE_TEMPLATE/feature_request.md`, `.github/dependabot.yml`, `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `.gitignore`
- Create: `server/pyproject.toml` (uv workspace-style project for `uv sync --project server`), `server/config.py`, `server/types.py`, `server/tests/__init__.py`, `server/tests/test_config.py`

**Interfaces:**
- Produces: `Config` with fields `api_token: str`, `data_dir: Path` (vault clone), `index_dir: Path`, `state_dir: Path`, `embed_url: str | None`, `embed_api_token: str | None`, `embed_model: str` (default `"embeddinggemma"`), `embed_dim: int` (default 768), `sync_mode: str` (default `"pull-only"`), `sync_timeout: int` (1800); `Config.load() -> Config` reads env. `NoteRow`, `EdgeRow`, `IndexResult`, `SyncResult` exactly as in Shared Interfaces.

- [ ] **Step 1: Write `server/tests/test_config.py`** — asserts: defaults (dim 768, model embeddinggemma, mode pull-only), env overrides via monkeypatch, `embed_url=None` when `VAULT_EMBED_URL` unset, token values round-trip. Run: `uv run --project server pytest server/tests/test_config.py` → FAIL (module missing).

- [ ] **Step 2: Implement `server/config.py` + `server/types.py`** exactly per Interfaces. Frontmatter/YAML not needed here. Include `pytest.ini_options` (asyncio_mode auto) in `server/pyproject.toml`, deps: `fastapi[standard]>=0.115`, `lancedb>=0.16`, `pyarrow>=17`, `httpx>=0.27`, `mcp>=1.0`, `modal>=0.64`, `pyyaml>=6`; dev group: `pytest`, `pytest-asyncio`, `ruff`.

- [ ] **Step 3: Scaffold meta files.** CI mirrors siblings: `uv sync --project server --group dev` → `uv run --project server pytest server/tests -q` → `uv run --project server ruff check server`. LICENSE Apache-2.0. `SECURITY.md` with the standard family report contact (GitHub private security advisory). README is Task 9, not now. `.gitignore`: `__pycache__/`, `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `.env`.

- [ ] **Step 4: Run** `uv run --project server pytest server/tests -q` → PASS; `uvx ruff@latest check server` → clean. Report.

### Task 2: store.py — LanceDB stores + watermarks-backed queries

**Files:**
- Create: `server/store.py`, `server/tests/test_store.py`

**Interfaces:**
- Consumes: `NoteRow`, `EdgeRow` from `server/types.py`.
- Produces: `VectorStore`, `GraphStore` exactly per Shared Interfaces. Table names: `notes__{model}__{dim}` and `edges`. Search results: `list[dict]` each with keys `id`, `text`, `frontmatter`, `score` (float, higher = better; `_distance` negated or converted, do not leak lancedb internals past this layer). `neighbors` returns `list[dict]` with `src`, `dst`, `kind`. Deletes buffered + applied on commit.

- [ ] **Step 1: Failing tests** (`server/tests/test_store.py`): round-trip upsert+counts; delete on commit; semantic/top-k ordering by cosine (insert known vectors, verify order); fts matches text; hybrid merges and dedupes by id (fts result kept when both match, prefer semantic score); commit amortization — `upsert_notes` twice without `commit` then one `commit` persists both; edges upsert + neighbors depth-2 dedupe; reset_edges empties. Use temp dir per test (tmp_path), no Volume.
- [ ] **Step 2: Run** → FAIL (no module).
- [ ] **Step 3: Implement** minimal; FTS via lancedb `create_index` FTS config; hybrid = run both then RRF-fuse scores in Python (rank-based 1/(60+rank) sum) — no lancedb hybrid API dependency.
- [ ] **Step 4: Run** → PASS; ruff clean. Report.

### Task 3: sync_service.py — `ob` orchestration (parallel-safe)

**Files:**
- Create: `server/sync_service.py`, `server/tests/test_sync_service.py`, `server/tests/fake_ob.py` (test helper writing an executable shell shim)

**Interfaces:**
- Consumes: `SyncResult` from types.
- Produces: `SyncService` per Shared Interfaces. Behavior: `is_logged_in` runs `ob whoami`, returncode 0 → True; `bootstrap(email, password)` runs `ob login` with stdin `email\npassword` (never argv, never logged); `one_shot` runs `ob sync --mode pull-only` with config timeout, records nothing to logs but stderr tail (last 500 chars) in `SyncResult.detail`; every invocation serialized by an internal threading.Lock (ob holds a workspace lock). All subprocess calls `capture_output=True, text=True`.

- [ ] **Step 1: Failing tests** with the fake `ob` shim on PATH (shim echoes argv to a file, exits 0; variants exit 1 / hang-briefly): whoami True/False; bootstrap passes creds via stdin only (shim asserts stdin content, argv has no password); one_shot ok → `SyncResult(ok=True, mode="pull-only", detail="")`; failure captures stderr tail; lock serializes (shim sleeps 0.2s, two threads, measure sequential order via argv-file ordering).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; ruff clean. Report.

### Task 4: linker.py — note-link graph from markdown (parallel-safe)

**Files:**
- Create: `server/linker.py`, `server/tests/test_linker.py`

**Interfaces:**
- Produces: `parse_links(text) -> list[str]`, `build_edge_set(vault_dir: Path, note_paths: list[str]) -> list[EdgeRow]` — resolves each link to a vault-relative note path: `[[wikilink]]`, `[[wikilink|alias]]`, `[label](relative.md)`; skips `http(s)://`, `obsidian://`, bare anchors; wikilinks match by case-insensitive stem OR suffix path (walks up directories: `[[idea]]` matches `projects/idea.md`; `[[projects/idea]]` suffix-matches too). `dst=""` when unresolvable (edge still emitted — honest dangling links). Self-links (src==dst) skipped. kind always `"note-link"`.

- [ ] **Step 1: Failing tests** on a fixture vault written to tmp_path: multiple notes, alias, stem-only match, suffix match, external skipped, dangling dst="", no self edge. Assert exact EdgeRow tuples.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (regex-based, whole-file scan).
- [ ] **Step 4: Run** → PASS; ruff clean. Report.

### Task 5: indexer.py — watermark + whole-note embed via hook

**Files:**
- Create: `server/indexer.py`, `server/tests/test_indexer.py`

**Interfaces:**
- Consumes: `Config`, `VectorStore`, `GraphStore`, `linker.build_edge_set`, `embed_texts`.
- Produces: `embed_texts(cfg, texts, task) -> list[list[float]] | None` and `sync_once(cfg, store, graph) -> IndexResult` per Shared Interfaces. Behavior: walk `cfg.data_dir` for `.md` (prune `.obsidian/`, `.trash/`); watermark JSON at `cfg.state_dir/watermark.json` maps path → sha256; changed/new notes → upsert (vector from hook `task="document"`, whole raw text embedded, frontmatter parsed via pyyaml); missing notes → `delete_note`; then edges rebuilt from scratch: `graph.reset_edges()` + upsert for changed notes only, `src_shas` passed so commit ordering is one `store.commit()` + one `graph.commit()` per pass (counts as amortized commit — assert in tests). Hook unset (`embed_url=None`) → return `IndexResult(skipped_reason="no embedding hook configured")` WITHOUT touching watermark (so a later configured run embeds everything). Hook HTTP failure → raise `HookError` (status + body tail); watermark update only after both commits succeed.

- [ ] **Step 1: Failing tests** with httpx `MockTransport` fake `/embed` endpoint (returns dim-3 vectors derived from text hash): first pass embeds all fixture notes (counts correct, watermark written, rows searchable); second pass no-op (all zero counts, zero HTTP calls — assert via request counter); edit one note → exactly 1 embed + 1 delete-then-upsert; delete a note → removed from store + watermark; hook unset → skipped, watermark untouched; hook 500 → HookError raised, watermark untouched, no commit (store spy records zero commits).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; ruff clean. Report.

### Task 6: mcp_tools.py — the five tools (parallel-safe against Task 5, contract fixed above)

**Files:**
- Create: `server/mcp_tools.py`, `server/tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `VaultTools.call(name, arguments) -> dict` with tools `vault.search`, `vault.read`, `vault.list`, `vault.query_graph`, `vault.status`. Exact response shapes (these are the wire contract; Task 7 tests assert them):
  - `vault.search(query: str, mode: "semantic"|"fts"|"hybrid"|"graph", k: int = 8)` → `{"results": [...], "mode": <effective>, "note": <optional honest-degrade-string>}`. Search results carry `id`, `text`, `frontmatter`, `score`. Degrade: hook unset and mode in (semantic, hybrid) → effective mode `fts`, `note: "semantic disabled: embedding hook not configured"`. `graph` mode = link-graph keyword walk: `graph.neighbors` seeded from notes whose text matches the query tokens (top 3 seeds by fts), depth 1; returns edges + seed ids.
  - `vault.read(path: str)` → `{"path", "text", "frontmatter", "exists": bool}`; 404 semantics: `exists: false`, not an error.
  - `vault.list(prefix?: str, tag?: str, collection?: str)` → `{"notes": [{"path", "frontmatter"}], "count"}`; tag filter = frontmatter `tags` list contains tag.
  - `vault.query_graph(seed: str, depth: int = 1, kind: str = "note-link")` → `{"edges": [...], "seeds": [...]}`; unknown kind → `{"error": "unknown graph kind"}` (MCP-level error reply).
  - `vault.status()` → `{"sync": {"mode", "last_sync_at", "ok"}, "index": {"notes", "edges"}, "embed_hook": {"configured": bool, "reachable": bool|None}}`. `last_sync_at` read from `cfg.state_dir/last_sync.json` (Task 7 boot writes it).

- [ ] **Step 1: Failing tests**: each tool against the real VectorStore/GraphStore (tmp dirs) + fake hook MockTransport + fake state dir; degrade note exact string; graph-mode walk returns edges; unknown kind error; list tag filter; status shape.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`TOOLS` list with JSON-schema `inputSchema` per tool; descriptions generic, no private names).
- [ ] **Step 4: Run** → PASS; ruff clean. Report.

### Task 7: web.py — FastAPI + MCP mount + admin (parallel-safe against Task 6, contract fixed above)

**Files:**
- Create: `server/web.py`, `server/tests/test_web.py`, `server/tests/conftest.py` (shared: `cfg` tmp-dir fixture, bearer header fixture)

**Interfaces:**
- Consumes: `VaultTools`, `build_mcp_server`. 
- Produces: `build_app(cfg) -> FastAPI`:
  - `GET /health` → `{"status": "ok"|"degraded", "vault": bool, "index": bool, "sync": {"mode", "last_sync_at", "ok"}}` (reads state dir; never raises).
  - `POST /admin/reindex` with bearer (401 without valid `Authorization: Bearer <VAULT_API_TOKEN>`) → clears watermark + runs `sync_once` inline → returns the `IndexResult` as JSON.
  - MCP server (official SDK low-level server, streamable-HTTP ASGI app) mounted at `/mcp` via `app.mount("/mcp", ...)`; an ASGI middleware checks the Bearer token on `/mcp/*` and `/admin/*` → 401 otherwise. Tool calls delegate to `VaultTools.call`.
  - Boot helper `run_boot(cfg, store, graph, sync) -> dict` = sequence: `sync.one_shot()` → on success write `last_sync.json`; `sync_once` (skip with degraded note when it raises); returns `{"sync": SyncResult, "index": IndexResult, "degraded": bool}`. Fail-closed: any boot failure leaves store/graph intact (they only mutate after successful commits).

- [ ] **Step 1: Failing tests** (Starlette `TestClient`): health all-states (ok, degraded-with-vault-missing, no-state); admin 401 → wrong token → 200 with reindex result (fake ob + fake hook fixtures from Tasks 3/5 pattern); MCP auth 401 without bearer; MCP list-tools and one tool call over the streamable-HTTP transport using the `mcp` SDK async client pointed at the ASGI app (this is the e2e proof; use `mcp` client's httpx-transport injection if available, else assert the mounted ASGI app object + tool handlers directly and mark the transport test with a clean skip only if the SDK cannot attach to an ASGI app — report which happened). Boot helper: fake ob succeeds → last_sync written, degraded False; ob fails → degraded True, index still built from last-good clone.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; ruff clean. Report.

### Task 8: app.py — Modal wiring

**Files:**
- Create: `server/app.py`, `server/tests/test_app.py`

**Interfaces:**
- Consumes: `build_app`, all modules.
- Produces: Modal App `modal-vault-server`: image `from_registry("node:24-bookworm-slim", add_python="3.12")` + `pip_install` deps + `npm install -g obsidian-headless@0.0.14` + `ob --version` check; volume `modal-vault` v2 (`/vault`, `/index`, `/state` via env `VAULT_DATA_DIR` etc. defaults); secrets `modal-vault-secret`; `@app.function(..., scaledown_window=300, timeout=3600, max_containers=1)` exposing `@modal.fastapi_endpoint()` entry `serve()` that runs `run_boot` then returns `build_app(cfg)`; secrets `VAULT_API_TOKEN` required — app fails closed at boot without it (loud error, no serving).

- [ ] **Step 1: Failing test** (`test_app.py`): import module, assert app name, volume name, and that `serve` requires the token (call the underlying boot-build path with missing token env → raises/exits; with token + fake dirs → FastAPI instance with `/mcp` mounted). No Modal deploy.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** `uv run --project server pytest server/tests -q` → all PASS; ruff clean. Report.

### Task 9: README + finalize

**Files:**
- Create: `README.md`

**Interfaces:**
- Produces: README with: title `# modal-vault-server`; one-line pitch (hosted vault + MCP memory plane); quick start (`uvx modal deploy server/app.py`, secret creation, `VAULT_EMBED_URL` pointing at modal-embedding-server, MCP client config sample — streamable HTTP URL `<workspace>--modal-vault-server.modal.run/mcp`, bearer `VAULT_API_TOKEN`); tool reference table (the 5 tools); family section: `## Part of the Modal Toolkit` — "Six standalone Modal utilities from the same author, each extractable and deployable on its own." + bullets for the six siblings (wording from their READMEs; this repo described as "Hosted vault + MCP memory plane: server-side Obsidian clone via Headless Sync with a searchable projection agents reach over MCP."); byline: `Built by [Kyle Brodeur](https://kylebrodeur.com) · Model-selection deep-dive: [Choose the Right Embedding Model for Your Data](https://kylebrodeur.substack.com/p/choose-embedding-model-for-your-data)`. NO private names, NO 1Password references, no iot-rig/govee.

- [ ] **Step 1: Write README.** 
- [ ] **Step 2: Full suite + ruff green; leak grep** `git grep -inE 'iot-rig|govee|hemmingway|1Password|kimi|glm5|vault-mind|pi-vault'` → empty. Report.

## Spec coverage check (written after plan draft)

- Boot/data-flow §3 → Tasks 5 (index), 7 (boot/run_boot), 8 (Modal wiring).
- MCP surface §4 → Task 6 (+ degrade) and 7 (mount/auth).
- Admin reindex §4 → Task 7.
- Non-goals §5 → constraints (pull-only, no ledgers, no chunking) enforced per task briefs.
- Fail-closed §6 → Task 7 boot helper + health degraded states.
- Amortized commits §6 → Task 2 commit design + Task 5 test.
- Testing §7 → each task's test steps; e2e MCP transport proof Task 7.
- Family integration → Task 9 (this repo's side; sibling-repo updates deferred to release day — out of plan scope, noted in spec).

## Controller-execution notes

- No worktrees: parallel implementers share the checkout; they must not commit (controller commits after review), must only touch their own task's files, and must run the suite scoped to `server/tests`.
- Wave plan: Task 1 → {Tasks 2, 3, 4 in parallel} → Task 5 → {Tasks 6, 7 in parallel} → Task 8 → Task 9 → final whole-branch review.
- Every task report goes to `<workspace>/task-<N>-report.md` per the SDD contract.