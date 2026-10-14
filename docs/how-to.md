# How to operate modal-vault-server

Current release: v1.5.0. Commands below come from the repo's
`server/mtk-commands.toml` and README at that tag. Start with the
[quickstart](./quickstart.md) if the vault is not deployed yet.

## Sync modes

The clone lives in the container at `/vault`. Every sync command runs
inside the container through `mtk vault`, so nothing syncs on your machine.

| Goal | Command |
| :--- | :--- |
| See the current sync state | `mtk vault status` |
| One-shot sync | `mtk vault sync` |
| Safe default: pull only | `mtk vault pull-only` |
| Push local writes back | `mtk vault sync-on-write` |
| Mirror the remote | `mtk vault mirror-remote` |
| Continuous loop | `mtk vault continuous` |
| List remote vaults you can sync | `mtk vault list-remote` |
| Tail the sync log | `mtk vault logs` |

Keep `pull-only` unless you have a reason to write back. It is the
default posture for a reason.

## Run any `ob` subcommand

`mtk vault exec` passes arguments through to `ob` inside the container:

```bash
mtk vault exec sync-config --mode pull-only --path /vault
```

## Write door

Writes are gated by the server's posture, not by the client. Deletion stays
off. The server accepts a deletion only inside an explicit, self-expiring
allow-delete window, and only after a successful pre-delete snapshot. If the
snapshot fails, the delete is refused. Treat any change to these gates as a
security change and verify it against a non-production vault first.

## Health and triage

`GET /health` reports vault and sync state. Use it before assuming a sync
problem: a cold start looks like a failure on the first request, so retry
once before you debug.

## Scale to zero

The app idles to zero containers. Waking it is automatic on the next
request. `mtk vault` commands wake the app first, so there is nothing to
warm by hand.

## Related

- Quickstart: [quickstart.md](./quickstart.md)
- Family overview: [modal-toolkit](https://github.com/kylebrodeur/modal-toolkit)
