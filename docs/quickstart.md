# Quickstart: modal-vault-server

Your Obsidian vault, served over MCP from Modal. Current release: v1.5.0.

Start with the [toolkit quickstart](https://github.com/kylebrodeur/modal-toolkit/blob/main/docs/quickstart.md) if `mtk` is not
installed yet. You need uv, git, and the Modal CLI (see its prerequisites).

## 1. Create a project

```bash
mtk init vault-lab        # named project; checks prerequisites and the account
cd vault-lab
```

`init` refuses a project id whose app names would collide with an existing Modal app.

## 2. Pull the package

```bash
mtk pull vault --tag v1.5.0
```

The package is vendored into `vendor/vault-server/`: a git checkout pinned to
the exact release tag, read-only and drift-guarded. Its provenance (repo, tag,
commit) is recorded beside it.

## 3. Write the overlay

Create `deploys/vault-server/` with two files. The app name must be
`<project>-vault`.

`deploys/vault-server/deploy.json`:

```json
{
  "pkg": "vault",
  "app_name": "vault-lab-vault",
  "upstream": { "repo": "modal-vault-server", "tag": "v1.5.0" }
}
```

`deploys/vault-server/deploy.sh`: the entry point. It deploys the VENDORED
copy under `vendor/vault-server/`: never a live checkout, never inside the
vendored tree. `mtk deploy vault` runs it after the preflight, drift, and
secrets gates.

## 4. Deploy

```bash
mtk deploy vault
```

`deploy` preflights the overlay, lists any missing secrets from the vault
manifest (`modal-vault-secret`, key `MODAL_VAULT_API_TOKEN`, generated), asks you
to confirm each one, and then runs `deploy.sh`.

## 5. Verify

```bash
mtk doctor --pkg vault
curl -s https://<your-workspace>--<app>-serve.modal.run/health
```

`/health` reports vault and sync state. A 503 or a cold start on the first call
is normal.

## 6. Connect an MCP client

The MCP endpoint is `/mcp` on the same host. Sign in with the operator token on
the OAuth consent page. Gemini Spark, Claude, and Cursor work through dynamic
client registration.

## Next

- Operations, sync modes, and the write door: [how-to](./how-to.md)
