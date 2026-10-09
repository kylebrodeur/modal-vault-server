# Setting up and using modal-vault-server

The end-to-end guide: from an empty Modal account to an agent reading
(and, in write postures, writing to) your Obsidian vault over MCP.
The [README](README.md) is the summary; this page is the how.

---

## 1. What you need

| Need | Where it comes from |
| :--- | :--- |
| A Modal account (CPU-only app; no GPU) | https://modal.com, then `modal setup` |
| An Obsidian Sync subscription (the headless client uses the same Sync backend) | your Obsidian account |
| A vault cloned by the server | Obsidian Headless (`ob`, installed in the container) does this at first boot |
| The `mtk` CLI (recommended: secrets + `vault` verbs) | https://github.com/kylebrodeur/modal-toolkit |
| A bearer token YOU generate | `openssl rand -hex 32` (the Secret holds it; nothing prints it) |

Cost posture: the app is scale-to-zero CPU. It wakes on connections,
sleeps when idle; only the Volume storage and wake-seconds bill.

---

## 2. One-time setup

### 2.1 Deploy

```bash
git clone https://github.com/kylebrodeur/modal-vault-server
cd modal-vault-server

uv sync --project server   # the project env carries fastapi/mcp/modal for the deploy parse
modal setup                # authenticate with Modal (one time)

PYTHONPATH=$PWD server/.venv/bin/modal deploy server/app.py
```

Deploy prints the app URL `https://<workspace>--modal-vault-server-serve.modal.run`.
The deploy creates the `modal-vault` Volume (v2) if missing and passes
health only after `ob --version` proves the binary.

Why `PYTHONPATH`: the Modal CLI loads the app file with `server/` on the
path (not the repo root), so `from server.config import ...` needs the
repo root on `PYTHONPATH`. Never run anything from INSIDE `server/`
(stdlib `types` collides with `server/types.py`).

### 2.2 The Secret

`modal-vault-secret` carries the bearer (`VAULT_API_TOKEN`, REQUIRED)
and, optionally, the first-boot `ob` credentials. Two ways:

```bash
# mt k (writes from the repo's server/secrets.toml manifest + prompts/generates):
mtk secrets check
mtk secrets create --pkg vault

# or by hand:
modal secret create modal-vault-secret \
  VAULT_API_TOKEN=$(openssl rand -hex 32) \
  VAULT_OB_EMAIL=you@example.com \
  VAULT_OB_PASSWORD='...' \
  VAULT_OB_MFA='' \
  VAULT_OB_VAULT='Personal' \
  VAULT_OB_E2E_PASSWORD=''
```

Key semantics:

- `VAULT_API_TOKEN` gates `/mcp` and `/admin/*`. Empty/unset = the app refuses to serve (fail-closed).
- `VAULT_OB_EMAIL`/`VAULT_OB_PASSWORD` enable SELF-bootstrapping: on
  boot, with fresh state, the app runs `ob login` (argv flags), `ob
  sync-setup --path /vault` (when `VAULT_OB_VAULT` is set), then `ob
  sync-config --mode pull-only`, then the first pull. Without them the
  app boots degraded-honest and waits for state to arrive by other
  means.
- `VAULT_OB_MFA`: only for accounts with MFA (an MFA CODE is login-time;
  TOTPs time out - prefer no-MFA accounts or service flows).
- `VAULT_OB_E2E_PASSWORD`: only for e2e-encrypted vaults (setup's
  `--password`).

Rotation note: Modal secrets CANNOT be read back. `mtk secrets rotate
modal-vault-secret` (or a `--force` create) regenerates it, which also
regenerates the bearer: every MCP client must pick up the new token,
and warm containers keep the old one until restarted.

### 2.3 Verify

```bash
curl https://<workspace>--modal-vault-server-serve.modal.run/health
# {"status":"ok","vault":true,"sync":{"mode":"pull-only","last_sync_at":"…","ok":true},
#  "sync_mode":"pull-only","continuous_running":false}
```

`status: ok` = login state + clone + watermark all landed. `degraded`
triage: `vault:false` = the clone dir missing (boot pull failed; check
the Secret's ob keys), `sync.ok:false` = watermark absent/stale (the
boot pull failed; `GET /admin/sync-mode` + container logs diagnose),
`boot.json` in the state dir names the failure TYPE (never values).

---

## 3. Connecting an MCP client

Any MCP-speaking client (streamable HTTP + bearer header):

```json
{
  "mcpServers": {
    "modal-vault": {
      "type": "http",
      "url": "https://<workspace>--modal-vault-server-serve.modal.run/mcp",
      "headers": {
        "Authorization": "Bearer <VAULT_API_TOKEN>"
      }
    }
  }
}
```

Client-matrix notes:

- pi/omp: put exactly the above config in the mcpServers section; the
  token comes from the Secret (read it via 1Password/Modal, never from
  chat).
- Claude Desktop/other stdlib-only clients: use an MCP streamable-HTTP
  bridge if HTTP+servers aren't supported natively.

### The MCP tool surface (v1: read-only)

| Tool | Purpose |
|------|---------|
| `vault.search` | Keyword search over the clone. mode `text` ranks live (every token must match; title hits weigh double); mode `graph` seeds a depth-1 walk from the top-3 matches and returns walked edges too |
| `vault.read` | One note's full text + frontmatter; missing notes return `exists: false` (not errors) |
| `vault.list` | Notes (path + frontmatter), filterable by directory prefix and tag |
| `vault.query_graph` | Link-graph neighborhood around a seed within `depth` hops (note-link edges) |
| `vault.status` | Sync watermark, live note count, the semantic-search door (constant `configured:false` until modal-embedding-server connects) |

Every reply is a plain JSON dict; errors degrade to `{"error": ...}`
(never raise). Search is a live scan (no index): clone = source of
truth; the link graph is a live walk.

### Operator verbs (mtk; run ob INSIDE the container, never locally)

```bash
mtk vault status        # ob sync-status --path /vault in the container
mtk vault sync          # one pull in the container
mtk vault pull-only     # set + persist mode pull-only (in the container)
mtk vault sync-on-write # set mode bidirectional (write door bursts)
mtk vault continuous    # run ob sync --continuous (real-time two-way)
mtk vault mirror-remote # emergency: mirror remote, RESTRICT local changes
mtk vault list-remote   # remote vaults the account sees
mtk vault list-local    # locally configured vaults
mtk vault config …      # ob sync-config passthrough
mtk vault logs          # tail the server-side sync log
mtk vault exec …        # raw ob passthrough
```

Each verb wakes the scale-to-zero app, finds its container, and execs
`ob` there with `XDG_CONFIG_HOME=/vault/state`. NOTHING ever syncs on
your machine; local sync defeats the server's purpose.

Admin REST (bearer-gated, same token) and the MCP write tools share one
core; the tool surface gains, when the write door is built (v2):

| Tool | Purpose |
|------|---------|
| `vault.create_note` | new note (path + full text); refuses existing paths |
| `vault.update_note` | full-text update of an existing note |
| `vault.snapshots` | the shadow-git log (the undo ladder; per-path) |
| `vault.revert` | restore one path from a snapshot sha |

`vault.delete_note` exists ONLY while the windowed delete door is armed
(`POST /admin/allow-delete {"action":"arm"}`); the door disarms itself
when the window expires or the container restarts, and every delete
requires a successful pre-delete snapshot (no snapshot, no delete).

### Admin REST (bearer-gated, same token)

| Route | What it does |
| :--- | :--- |
| `GET /health` | public triage (no token) |
| `POST /admin/sync` | one `ob` pull now |
| `POST /admin/sync-mode` | runtime posture flip: `{"mode": "pull-only"\|"sync-on-write"\|"continuous"}` |
| `GET /admin/sync-mode` | current posture + whether the continuous daemon is live |
| `POST /admin/notes` | create-or-update: `{"path","text","agent"}` (snapshot-first, posture-aware sync) |
| `POST /admin/notes/delete` | delete — **only while the delete window is armed** |
| `POST /admin/allow-delete` | arm/disarm/inspect the windowed delete door: `{"action":"arm"\|"disarm"\|"status","window_minutes":60}` |

The flip is a RUNTIME decision persisted in the state dir
(`sync-mode.json`): no env change, no redeploy, survives container
restarts. `continuous` starts an owned `ob sync --continuous` daemon;
the other postures stop it (the boot re-applies the persisted mode).
`sync-on-write` means: every write does one serialized pull/push burst
(no daemon between writes). In `pull-only`, writes are staged locally
and the reply says so.

---

## 4. Postures: what each sync mode means

- **pull-only** (default, the v1 safety posture): every sync downloads
  remote changes and `sync-config --mode pull-only` makes it durable -
  the server clone can never push. Combined with the server having NO
  write tools in v1, agents cannot alter your vault through this app.
- **sync-on-write**: for the write door (when enabled server-side):
  each create/update writes the file, snapshot-commits, then one
  serialized `ob sync` burst (push + pull). No continuous process.
- **continuous**: real-time two-way; an `ob --continuous` daemon owns
  the container (single-writer discipline rides the SyncService lock).
  Cost: the container stays warm while the daemon runs.

Obsidian Sync keeps its own server-side version history (your plan's
retention; device names appear in it) - that is the built-in undo
layer, restorable today from the desktop app.

---

## 5. Volume layout

One Modal Volume (v2), everything on it:

```
/vault                 (VAULT_DATA_DIR; the clone)
├── 00-system/…        (your vault's folders - the clone IS the truth)
└── state/
    ├── obsidian-headless/   (ob login state + sync config; the XDG root)
    ├── last_sync.json       (the watermark: {"mode","last_sync_at","ok"})
    ├── sync-mode.json       (the runtime posture: {"mode","set_at","by"})
    └── boot.json            (boot-failure notes, type-only)
```

`/state` is a symlink to `/vault/state` (Modal forbids mounting one
Volume at two roots). The state living ON the volume is what makes cold
boots serve without re-login.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `health` 401 on `/admin/*` or MCP | wrong/old bearer | read the CURRENT token from the Secret (rotations regenerate it); restart warm containers after rotation |
| App stuck `initializing`, URLs 404 | missing Secret/keys | check `modal-vault-secret` exists with `VAULT_API_TOKEN` |
| `status degraded`, `vault:true`, `sync.ok:false` | the boot pull failed | `mtk vault status` + `mtk vault logs`; re-run `mtk vault sync` |
| boot pull fails with argv errors | credentials missing (no secret ob keys) | add `VAULT_OB_*` keys (+ `--force` recreate; all bearers rotate) |
| login "succeeds" but nothing persists | you piped login via stdin | use argv flags (the server does this already) |
| `sync` errors mention `--mode` | something passed `--mode` to `sync` (invalid) | mode belongs to `sync-config`; `mtk vault pull-only` |
| clone exists but is empty | first pull hasn't finished | the first pull takes ~100s for big vaults; watch `mtk vault status` |
| `XDG`-state lost after deploy | the state dir wasn't on the Volume | verify the `/state -> /vault/state` symlink + the Volume mount in `modal app logs` |

---

## 7. Integration: hooks, overlays, and upgrades (for lanes building on this)

Lanes that stand up their own instance of this server (a project, an
agent team) integrate WITHOUT touching upstream code:

- **The three options, in order of preference:**
  1. *Deploy upstream directly.* Set the Secret + env overrides
     (`MODAL_VAULT_APP_NAME`-style names are in the config layer); no
     code of your own. Adopt upstream upgrades by pulling + redeploying.
  2. *Ship a deploy overlay* (your own repo dir referencing this
     checkout): set instance env (app name / secret names), and use
     **`server/hooks`** to hook IN — never monkeypatch upstream modules.
     Hooks are named lifecycle functions:
     - `hooks.register("boot.pre", fn(cfg, sync))` — runs before
       login/pull on every boot.
     - `hooks.register("boot.post", fn(cfg, report))` — runs after boot
       with the health-shaped report.
     - `hooks.register("write.post", fn(report))` — runs after every
       MCP/REST write/delete/revert with the full write report.
     Multiple hooks coexist (you can build ON TOP of another lane's
     hook); a hook's errors are contained and reported (they never break
     boot or writes) — the tag list is closed; new tags are upstream
     work.
  3. *Consume the surfaces as a client*: MCP tools for agents; the admin
     REST routes for scripts. No deploy of your own.
- **Overridable knobs** (config file / env, per `docs/SETUP-AND-MCP.md`):
  the app name, the Secret name, the Volume name, ob-credential env
  names. `VAULT_OB_*` accepts the short `OB_*` names too, so existing
  lanes' Secrets work without remapping.
- **Upgrading an overlay lane** (the writing-duo vault-ob pattern): when
  upstream grows the behavior you monkeypatched, retire your patch and
  register the remainder as hooks (or nothing at all). Then: pull
  upstream, run your deploy script, verify `/health` + your lane's
  verbs. Upstream tags stay the upgrade unit; breaking hook changes are
  called out in release notes.

---

## 8. What's next

Shipped since v1.1.0: runtime-mutable sync postures (`/admin/sync-mode`),
the shadow-git snapshot layer, the MCP write tools +
`POST /admin/notes`, and the windowed delete door. Open work: hybrid
semantic ranking (the `vault.status` door is live when `VAULT_EMBEDDING_URL`
is set; ranking over it comes next) and the ledger/memory-plane door.