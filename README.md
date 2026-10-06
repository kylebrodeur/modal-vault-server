# modal-vault-server

Hosted Obsidian vault + MCP memory plane on Modal; agents connect over MCP (read-only in v1).

![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/Python-3.12-blue.svg)
[![Runs on Modal](https://img.shields.io/badge/Runs%20on-Modal-green.svg)](https://modal.com)
[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-pink.svg)](https://github.com/sponsors/kylebrodeur)

## What it is

A single scale-to-zero Modal CPU app keeps a server-side clone of your Obsidian vault current via Headless Sync (`ob`, pull-only in v1) and serves that clone to any MCP-speaking agent as read-only memory tools. There is no vector index in this repo: keyword search is a live scan of the clone and the link graph is a live walk, so the clone itself is the only source of truth. Semantic search arrives by connecting [modal-embedding-server](https://github.com/kylebrodeur/modal-embedding-server) later; this repo stays vault + MCP only.

Alongside the MCP mount the app exposes `GET /health` (vault + sync triage) and a bearer-gated `POST /admin/sync` (one extra `ob` pull on demand).

## The five MCP tools

| Tool | Purpose |
|------|---------|
| `vault.search` | Keyword search over the clone. mode `text` ranks notes live (every query token must match; title hits weigh double). mode `graph` seeds a depth-1 link-graph walk from the top-3 text matches and also returns the walked edges. |
| `vault.read` | One note's full text and frontmatter; missing notes return `exists: false`, not an error. |
| `vault.list` | Notes (path + frontmatter), optionally filtered by directory prefix and frontmatter tag. |
| `vault.query_graph` | Link-graph neighborhood around one seed note within `depth` hops (kind `note-link` only in v1). |
| `vault.status` | Sync watermark, live note count, and the semantic-search door. |

Honesty rules the tool surface: every reply is a plain JSON dict, errors degrade to `{"error": ...}` instead of raising, and `vault.status` reports the semantic door as a constant (`"configured": false` with a note pointing at the embedding-server connection) until that connection actually exists. There is no semantic or hybrid mode here, and none stored or computed.

## Quick start

```bash
modal setup  # one-time: authenticate with Modal

modal secret create modal-vault-secret VAULT_API_TOKEN=<choose-a-long-random-token>

uvx modal deploy server/app.py
```

Deploy prints the web URL, e.g. `https://<workspace>--modal-vault-server-serve.modal.run`.

MCP client config sample (streamable HTTP):

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

The bearer gate covers `/mcp` and `/admin/*`; an empty `VAULT_API_TOKEN` refuses to serve entirely (fail-closed). First-boot note: the `ob` login state persists on the Volume, but the login bootstrap is not wired into v1 serving; until a logged-in clone exists, `/health` reports degraded and tools see an empty clone.

## Configuration

Every knob uses the `VAULT_` prefix and is read from env inside the container (the Modal Secret supplies `VAULT_API_TOKEN`).

| Variable | Description | Default |
|----------|-------------|---------|
| `VAULT_API_TOKEN` | Bearer token gating `/mcp` and `/admin/*`; required, never empty in serving | unset (refuses to serve) |
| `VAULT_DATA_DIR` | Vault clone root | `/vault` |
| `VAULT_STATE_DIR` | `ob` login state + sync watermark | `<data_dir>/state` |
| `VAULT_SYNC_MODE` | Sync direction for the `ob` pull | `pull-only` |
| `VAULT_SYNC_TIMEOUT` | Seconds before a one-shot pull times out | `1800` |

Volume layout: one Modal Volume (v2) carries everything. `/vault` is the clone; `/state` (login state + watermark) is a symlink into `/vault/state` because Modal forbids mounting one Volume at two roots.

## Slice 2 roadmap

Explicitly not built. Candidates, in no committed order:

- `vault.write` + `ob push` write-back: edit the clone server-side, deliver the edit back through Obsidian Sync.
- Persistent `syncd` option: split the sync loop from serving so the clone stays always-current (the loop is already its own module; this changes the cost posture).
- Semantic search: connect modal-embedding-server for embeddings/hybrid ranking; the `vault.status` door flips from constant to live only then.
- Ledger/memory plane door: durable agent ledgers as integration hooks, not stored in this repo.

## Part of the Modal Toolkit

Six standalone Modal utilities from the same author, each extractable and deployable on its own.

- **[modal-embedding-server](https://github.com/kylebrodeur/modal-embedding-server):** GPU-backed embeddings with a monotonic sync protocol for private-first search.
- **[modal-inference-server](https://github.com/kylebrodeur/modal-inference-server):** OpenAI-compatible LLM inference with hot-set routing and scale-to-zero.
- **[modal-vision-server](https://github.com/kylebrodeur/modal-vision-server):** Generic vision classification: pick your model (open_clip or transformers weights), your segmenter (SAM 2.1 or none), and your fast gate (self, cheap CLIP, deterministic script, or external endpoint). The BioCLIP plant stack ships as the example card.
- **[modal-finetune-server](https://github.com/kylebrodeur/modal-finetune-server):** Profile-driven LoRA fine-tune and GGUF pipeline with an honest eval gate.
- **[modal-toolkit](https://github.com/kylebrodeur/modal-toolkit):** One operator CLI (`mtk`) that runs the fleet: `doctor`, `warm --all`, `shutdown --all`, `cost`, `flow`.
- **[embed-eval-on-your-vault](https://github.com/kylebrodeur/embed-eval-on-your-vault):** the eval-first pattern (benchmark embedding models on your own data before you deploy) as a single-file, zero-dependency harness.
- **[modal-vault-server](https://github.com/kylebrodeur/modal-vault-server):** Hosted vault + MCP memory plane: server-side Obsidian clone via Headless Sync with searchable state agents reach over MCP.

## Built on Modal

These packages run on [Modal](https://modal.com), the serverless GPU platform (this one is CPU-only, but it deploys the same way). If you build something with them, share it in the [Modal Slack](https://modal.com/slack) community (`#show-and-tell`). Issues and PRs welcome here on GitHub.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for ground rules and workflow.

---

Built by [Kyle Brodeur](https://kylebrodeur.com) · Model-selection deep-dive: [Choose the Right Embedding Model for Your Data](https://kylebrodeur.substack.com/p/choose-embedding-model-for-your-data)

## License

Apache-2.0: see [LICENSE](LICENSE).