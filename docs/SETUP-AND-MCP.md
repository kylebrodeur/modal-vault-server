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

`modal-vault-secret` carries the bearer (`MODAL_VAULT_API_TOKEN`, REQUIRED)
and, optionally, the first-boot `ob` credentials. Two ways:

```bash
# mt k (writes from the repo's server/secrets.toml manifest + prompts/generates):
mtk secrets check
mtk secrets create --pkg vault

# or by hand:
modal secret create modal-vault-secret \
  MODAL_VAULT_API_TOKEN=$(openssl rand -hex 32) \
  MODAL_VAULT_OB_EMAIL=you@example.com \
  MODAL_VAULT_OB_PASSWORD='...' \
  MODAL_VAULT_OB_MFA='' \
  MODAL_VAULT_OB_VAULT='Personal' \
  MODAL_VAULT_OB_E2E_PASSWORD=''
```

Key semantics:

- `MODAL_VAULT_API_TOKEN` gates `/mcp` and `/admin/*`. Empty/unset = the app refuses to serve (fail-closed).
- `MODAL_VAULT_OB_EMAIL`/`MODAL_VAULT_OB_PASSWORD` enable SELF-bootstrapping: on
  boot, with fresh state, the app runs `ob login` (argv flags), `ob
  sync-setup --vault <MODAL_VAULT_OB_VAULT> --path /vault --device-name
  modal-vault-server` (when `MODAL_VAULT_OB_VAULT` is set), then `ob
  sync-config --mode pull-only`, then the first pull. Without them the
  app boots degraded-honest and waits for state to arrive by other
  means.
- `MODAL_VAULT_OB_MFA`: only for accounts with MFA (an MFA CODE is login-time;
  TOTPs time out - prefer no-MFA accounts or service flows).
- `MODAL_VAULT_OB_E2E_PASSWORD`: only for e2e-encrypted vaults (setup's
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
        "Authorization": "Bearer <MODAL_VAULT_API_TOKEN>"
      }
    }
  }
}
```

Client-matrix notes:

- `scripts/vault-mcp-install.sh` writes the entry through each harness's
  NATIVE surface, so you never hand-maintain five config copies:

  ```bash
  scripts/vault-mcp-install.sh --client codex   [--url URL]   # export MODAL_VAULT_API_TOKEN; codex reads it at runtime
  scripts/vault-mcp-install.sh --client claude  [--url URL]   # token via ${MODAL_VAULT_API_TOKEN} expansion
  scripts/vault-mcp-install.sh --client json    [--url URL] [--token TOKEN]  # generic mcpServers payload
  scripts/vault-mcp-install.sh --client gh                    # interactive copilot guidance
  scripts/vault-mcp-install.sh --client <any> --check         # dry-run, write nothing
  scripts/vault-mcp-install.sh --client <any> --remove        # remove the entry
  ```

  Codex mode never takes a literal token: it registers
  `--bearer-token-env-var MODAL_VAULT_API_TOKEN`, so export the token. Claude
  mode writes `${MODAL_VAULT_API_TOKEN}` expansion into its config file.
  `scripts/mcp-config.example.json` is the json-mode template (placeholder
  workspace + token; never a real value).
- pi/omp: put exactly the above config in the mcpServers section; the
  token comes from the Secret (read it via 1Password/Modal, never from
  chat). omp mounts the json-mode payload harness-side; pi imports it
  through its extension surface (pi has no built-in MCP).
- Claude Desktop/other stdlib-only clients: use an MCP streamable-HTTP
  bridge if HTTP+servers aren't supported natively.

### OAuth 2.1 clients (Claude, Cursor, Gemini Spark, Inspector)

Static bearer requires the client to accept a hardcoded header. Most of the
MCP ecosystem instead expects OAuth 2.1: discover the protected-resource
metadata, discover the authorization server, register a client, run an
authorization-code + PKCE flow in a browser, and present the bearer access
token. To serve those clients, set `MODAL_VAULT_MCP_AUTH=both` (static token
keeps working) or `oauth` (OAuth only), and set `MODAL_VAULT_MCP_AUTH_ISSUER`
to the app's origin (e.g.
`https://<workspace>--modal-vault-server-serve.modal.run`). No other config:
the authorization server is self-hosted in the app.

What a client sees:

- `GET /.well-known/oauth-protected-resource/mcp` → this resource + the issuer.
- `GET /.well-known/oauth-authorization-server` → endpoints + scopes + DCR.
- `POST /register` → Dynamic Client Registration; the client supplies its own
  redirect URIs, so Gemini Spark's per-connector
  `https://oauth-redirect.googleusercontent.com/r/<id>` callback is accepted
  without a server-side allowlist (the flow is client-agnostic).
- `GET /authorize` → redirects to the in-app consent page. The operator pastes
  the vault's `MODAL_VAULT_API_TOKEN` once; the page then returns the
  authorization code to the client's redirect URI.
- `POST /token` (PKCE S256) → short-lived access token + rotating refresh.
- `POST /revoke` → kills an access or refresh token. Public clients must send
  the `client_secret` form field (empty string is fine) - the SDK's revocation
  handler requires the field present.

Tokens are opaque and stored only as SHA-256 digests on the Volume
(`/vault/state/mcp-as/`); access tokens live 1h, refresh tokens rotate on use,
and `/mcp` refuses any token whose resource indicator is not this server.
Scope is per tool call: a `vault.read`-only grant cannot call a write tool.

**Gemini Spark:** `gemini.google.com` → Settings → Connected Apps → Custom
apps → Add a custom app → enter the `/mcp` URL → approve the consent page.

### The MCP tool surface

| Tool | Kind | Purpose |
|------|------|---------|
| `vault.search` | read | Keyword search over the clone. mode `text` ranks live (every token must match; title hits weigh double); mode `graph` seeds a depth-1 walk from the top-3 matches and returns walked edges too |
| `vault.read` | read | One note's full text + frontmatter; missing notes return `exists: false` (not errors) |
| `vault.list` | read | Notes (path + frontmatter), filterable by directory prefix and tag |
| `vault.query_graph` | read | Link-graph neighborhood around a seed within `depth` hops (note-link edges) |
| `vault.status` | read | Sync watermark, live note count, the semantic-search door (`configured:false` until modal-embedding-server connects) |
| `vault.create_note` | write | new note (path + full text); refuses existing paths; snapshot-first |
| `vault.update_note` | write | full-text update of an existing note; snapshot-first |
| `vault.snapshots` | write | the shadow-git log (the undo ladder; per-path) |
| `vault.revert` | write | restore one path from a snapshot sha; snapshots the revert itself |

The MCP registry advertises these nine tools. The four write tools are
posture-aware: in `pull-only` a write is staged locally and the reply
says so; in `sync-on-write`/`continuous` it syncs per the posture.
Every reply is a plain JSON dict; errors degrade to `{"error": ...}`
(never raise). Search is a live scan (no index): clone = source of
truth; the link graph is a live walk.

Delete is NOT an advertised MCP tool. It rides the bearer-gated
`POST /admin/notes/delete` route, which works ONLY while the windowed
delete door is armed (`POST /admin/allow-delete {"action":"arm"}`); the
door disarms itself when the window expires or the container restarts,
and every delete requires a successful pre-delete snapshot (no
snapshot, no delete).

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
core.

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

- **pull-only** (default, the safety posture): every sync downloads
  remote changes and `sync-config --mode pull-only` makes it durable -
  the server clone can never push. The write tools still exist, but in
  this posture a write is staged locally and the reply says so: nothing
  an agent writes reaches your remote vault until the posture changes.
- **sync-on-write**: each create/update writes the file,
  snapshot-commits, then one serialized `ob sync` burst (push + pull).
  No continuous process.
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
/vault                 (MODAL_VAULT_DATA_DIR; the clone)
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
| App stuck `initializing`, URLs 404 | missing Secret/keys | check `modal-vault-secret` exists with `MODAL_VAULT_API_TOKEN` |
| `status degraded`, `vault:true`, `sync.ok:false` | the boot pull failed | `mtk vault status` + `mtk vault logs`; re-run `mtk vault sync` |
| boot pull fails with argv errors | credentials missing (no secret ob keys) | add `MODAL_VAULT_OB_*` keys (+ `--force` recreate; all bearers rotate) |
| login "succeeds" but nothing persists | you piped login via stdin | use argv flags (the server does this already) |
| `sync` errors mention `--mode` | something passed `--mode` to `sync` (invalid) | mode belongs to `sync-config`; `mtk vault pull-only` |
| clone exists but is empty | first pull hasn't finished | the first pull takes ~100s for big vaults; watch `mtk vault status` |
| `XDG`-state lost after deploy | the state dir wasn't on the Volume | verify the `/state -> /vault/state` symlink + the Volume mount in `modal app logs` |
| `modal volume put` writes not visible to a running container (or clobbered) | an already-running container holds its own mount view and commits over CLI puts on shutdown | CLI puts need a container restart to be seen; server-side writes are the durable path |
| OAuth client can't connect / `register` 404 | `MODAL_VAULT_MCP_AUTH` left at `token` | set `both` (or `oauth`) and redeploy; `GET /.well-known/oauth-authorization-server` should then 200 |
| OAuth authorize redirects with `invalid_request: unknown resource` | client asked for a `resource` that isn't this server's `/mcp` | use the `/mcp` URL as the resource; the token is bound to it |
| Consent page always says "did not match" | wrong operator token | paste the CURRENT `MODAL_VAULT_API_TOKEN` (rotations change it) |

---

## 7. Integration: hooks, overlays, and upgrades (for lanes building on this)

Lanes that stand up their own instance of this server (a project, an
agent team) integrate WITHOUT touching upstream code:

- **The three options, in order of preference:**
  1. *Deploy upstream directly* — for STANDALONE public users only. Set
     the Secret + the `MODAL_VAULT_*` runtime knobs; the app keeps its default
     instance names (`server/app.py` fixes the app, Secret, and Volume
     names in code). Adoption path: pull the tag + redeploy.
  2. *Ship a deploy overlay* (your own repo dir referencing this
     checkout) — for LANES sharing a workspace with other instances. The
     overlay composes a distinct app name + secret names (the instance
     names are deployment wiring there, not config); use
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
     work. The decorator form reads best in an overlay:

     ```python
     from server import hooks

     @hooks.on("write.post")
     def provenance(report: dict) -> None:
         # every write/delete/revert lands here with the full report
         ...
     ```
  3. *Consume the surfaces as a client*: MCP tools for agents; the admin
     REST routes for scripts. No deploy of your own.
- **Runtime knobs** (env, read in the container): the `MODAL_VAULT_*` variables
  in §Configuration. The ob-credential knobs accept the short `OB_*`
  names too (`MODAL_VAULT_OB_*` wins when both are set), so an existing lane's
  Secrets work without remapping. The app / Secret / Volume NAMES are
  NOT runtime-overridable; a lane that needs distinct names uses an
  overlay (§7 above).
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
semantic ranking (the `vault.status` door is live when `MODAL_VAULT_EMBEDDING_URL`
is set; ranking over it comes next) and the ledger/memory-plane door.