# modal-vault-server Implementation Plan (AMENDED: vault + MCP only)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `modal-vault-server` — an Obsidian vault host on Modal with an MCP memory plane for agents: Headless Sync clone + on-demand vault tools (read/list/keyword-search/link-graph).

**Architecture:** Single scale-to-zero Modal CPU app: `ob` pull-sync on boot → serve the clone over MCP at `/mcp` (plus `/health`, bearer `/admin/sync`). NO vector index, NO embeddings: keyword search is a live scan of the clone; graph is a live link walk. Semantic/embedding later = connect the modal-embedding-server ecosystem (toolkit-managed, integration hook documented as a door only).

**Tech Stack:** Python 3.11+, uv, pytest, ruff, FastAPI, official `mcp` SDK (streamable HTTP), Modal; `ob` (npm `obsidian-headless@0.0.14`) as an image-installed subprocess binary. (LanceDB/PyArrow/httpx removed from deps after amendment.)

**Spec:** `docs/superpowers/specs/2026-10-06-modal-vault-server-design.md` (amendment below overrides spec sections 2-5)

## Amendment (2026-10-06, user-directed; binds over the spec's indexing sections)

- modal-vault-server = **vault + MCP only**: no embeddings, no vector index, none stored, none computed.
- `vault.search` modes: `"text"` (live keyword scan, ranked) and `"graph"` (link-graph walk seeded by text matches). NO semantic/hybrid modes in this repo.
- `Config` drops: embed_url, embed_api_token, embed_model, embed_dim, index_dir. Keeps: api_token, data_dir, state_dir, sync_mode, sync_timeout.
- `types.py` drops NoteRow. Keeps SyncResult; IndexResult repurposed = `notes_scanned: int, edges_upserted: int, skipped_reason: str | None`.
- `/admin/reindex` is GONE; replace with `POST /admin/sync` (bearer) = one-shot pull + re-scan graph cache-free state.
- Later-door contract (documented in README, not code): semantic search arrives by pointing vault.search at the embedding-server ecosystem via a future `VAULT_SEMANTIC_URL` hook; the response-shape door stays honest (`note:` field).

## Global Constraints

Same as the original plan, minus the indexing/embedding lines:
- Python 3.11+ only; no TS/node runtime code; `ob` is image-installed subprocess.
- Every env knob uses the `VAULT_` prefix.
- **No embedding/vector/index machinery anywhere in this repo.**
- Bearer auth on `/mcp` and `/admin/*` via `VAULT_API_TOKEN`.
- Slice-1 sync is pull-only; no write-back.
- Fail-closed serving: sync failure at boot leaves `/mcp` + `/health` serving last-good clone with degraded status.
- **Parallel-execution rule:** implementers write files and run scoped tests but DO NOT run `git commit`; controller commits after review.
- Conventional Commits; no em dashes. Tests: `server/tests/test_<module>.py`, deterministic, no network, no Modal deploy in CI.

## Shared Interfaces

```python
# server/types.py (Task 1, amended)
@dataclass(frozen=True) class EdgeRow:
    src: str           # vault-relative path
    dst: str           # vault-relative path (resolved; "" when dangling)
    kind: str          # "note-link" in slice 1
    raw: str           # raw link text as written

@dataclass class IndexResult:
    notes_scanned: int = 0
    edges_upserted: int = 0
    skipped_reason: str | None = None

@dataclass class SyncResult:
    ok: bool
    mode: str          # "pull-only"
    detail: str        # "" on success, stderr excerpt (tail 500) on failure
```

```python
# server/sync_service.py (Task 3)
class SyncService:
    def __init__(self, workspace_dir: Path, state_dir: Path, ob_bin: str = "ob") -> None
    def is_logged_in(self) -> bool
    def bootstrap(self, email: str, password: str) -> None
    def one_shot(self, timeout_seconds: int = 1800) -> SyncResult
```

```python
# server/linker.py (Task 4)
def parse_links(text: str) -> list[str]
def build_edge_set(vault_dir: Path, note_paths: list[str]) -> list[EdgeRow]
```

```python
# server/search_scan.py (Task 6a; replaces store.py+indexer.py)
def scan_vault(vault_dir: Path, query: str, k: int = 8, prefix: str = "") -> list[dict]
    # live ranked keyword scan; each dict: {"id": path, "text": full text, "frontmatter": dict, "score": float}
def collect_notes(vault_dir: Path, prefix: str = "") -> list[dict]
    # {"path", "text", "frontmatter"} for every .md (prunes .obsidian/, .trash/)
def frontmatter_of(text: str) -> dict
```

```python
# server/mcp_tools.py (Task 6)
class VaultTools:
    def __init__(self, cfg: Config, search: SearchAdapter, sync: SyncService) -> None
    async def call(self, name: str, arguments: dict) -> dict
    TOOLS: list[dict]
```

```python
# server/web.py (Task 7)
def build_app(cfg: Config) -> FastAPI
def build_mcp_server(cfg: Config, tools: VaultTools) -> object
def run_boot(cfg: Config, sync: SyncService) -> dict   # no index phase anymore
```

---

### Task 1: Repo scaffold + config + shared types — COMPLETE + AMENDED (ImplT1c doing the config/types amendment now; controller commits after)

### Task 2: CANCELLED (was LanceDB stores — no longer exists in scope)

### Task 3: sync_service.py — RUNNING unchanged (brief stands as-is)

### Task 4: linker.py — RUNNING unchanged (brief stands; serves query_graph live)

### Task 6: mcp_tools.py + search_scan.py (vault tools; read-only)

**Files:**
- Create: `server/search_scan.py`, `server/mcp_tools.py`, `server/tests/test_search_scan.py`, `server/tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: amended `Config` (data_dir, state_dir, api_token), `linker.build_edge_set`, `SyncService` (status only).
- Produces: tool wire contract:
  - `vault.search(query: str, mode: "text"|"graph", k: int = 8, prefix?: str)` → `{"results": [{"id","text","frontmatter","score"}...], "mode": <effective>, "note": <honest-degrade-string|None>}`. `graph` mode: top-3 text matches seed `linker`-built edge walk depth 1; returns edges + seed ids.
  - `vault.read(path: str)` → `{"path","text","frontmatter","exists": bool}` (exists:false, not an error).
  - `vault.list(prefix?, tag?)` → `{"notes": [{"path","frontmatter"}], "count"}`; tag = frontmatter tags contains.
  - `vault.query_graph(seed: str, depth: int = 1, kind: str = "note-link")` → `{"edges":[...], "seeds":[...]}`; unknown kind → error reply.
  - `vault.status()` → `{"sync": {"mode","last_sync_at","ok"}, "notes": <count>, "semantic": {"configured": false, "note": "connect modal-embedding-server later"}}`.

- [ ] **Step 1: Failing tests** for scan_vault ranking (multi-token, case-insensitive, title-boost), collect_notes pruning, frontmatter parse; VaultTools each tool, graph walk, unknown-kind error, status shape.
- [ ] **Step 2: Run → FAIL. Step 3: Implement. Step 4: full server/tests green + ruff. Report.**

### Task 7: web.py — FastAPI + MCP mount + admin sync + boot

**Files:**
- Create: `server/web.py`, `server/tests/test_web.py`, `server/tests/conftest.py`

**Interfaces:**
- `GET /health` → `{"status": "ok"|"degraded", "vault": bool, "sync": {...}}` (never raises).
- `POST /admin/sync` bearer-gated (401 otherwise) → runs `sync.one_shot()` → `SyncResult` JSON.
- MCP low-level server mounted at `/mcp` (streamable HTTP) behind ASGI bearer middleware; tools delegate to `VaultTools.call`.
- `run_boot(cfg, sync)`: `one_shot()`; success → write `last_sync.json`; failure → degraded flag, never raise, clone untouched (last-good).
- [ ] Same TDD steps; MCP transport e2e via the `mcp` SDK async client against the mounted ASGI app, or clean documented skip + direct-handler proof (report which).

### Task 8: app.py — Modal wiring (simplified)

- No index volume dir, no embed hook env. Volume `modal-vault` v2: `/vault` + `/state`. Image identical (ob binary). `serve()` = run_boot then build_app. Fail-closed at boot without `VAULT_API_TOKEN` (loud, no serving).
- [ ] Same TDD steps (no-deploy import/config test).

### Task 9: README + finalize

- Pitch: hosted vault + MCP; NO semantic claims; door sentence: "Semantic search over this vault arrives when you connect the modal-embedding-server ecosystem; this repo stays vault + MCP only."
- Tool table for the five tools (search=text/graph). Family section: seven repos listed; the six sibling bullets + this one new. Byline per family. Leak grep clean.
- [ ] Write; full suite + ruff; leak grep; report.

## Spec coverage (amended)

- Boot/data-flow (amended: sync only) → T7 run_boot + T8 serve.
- MCP read surface → T6; mount/auth → T7; admin sync → T7.
- Fail-closed → T7 + T8. Pull-only → T3.
- NO-index scope → enforced by amended interfaces; Task 2 killed; deps pruned.
- Family integration → T9.

## Controller-execution notes

- Parallel waves: {T3, T4} running now → amendment of T1 files by ImplT1c concurrently (config.py/types.py only) → T6 → T7 → T8 → T9 → final review.
- Original plan file's Task 2/5 and interface names (store.py/indexer.py/NoteRow) are void; amendment section here is the binding text.