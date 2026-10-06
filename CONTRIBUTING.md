# Contributing

Thanks for your interest in contributing. This project is extracted from production work and kept intentionally lean, so contributions should follow the same spirit.

## Ground Rules

- **Keep it boring.** Prefer straightforward code over clever abstractions. This is a utility, not a framework.
- **No new heavyweight deps** unless absolutely required for the sync or index path. The local test env must stay fast to `uv sync`.
- **Env prefixes** follow `VAULT_*`. Never reintroduce project-specific branding.
- **Amortized commits.** Store mutations buffer and land in one LanceDB commit per index pass; do not introduce per-row commits.
- **Fail-closed serving.** Boot failures must leave `/mcp` and `/health` serving last-good state; never crash the read path on sync errors.
- **No AI slop.** Comments and docs should describe *why* the code exists, not restate what it does.

## Workflow

1. Fork and create a feature branch.
2. Use `uv sync --project server --group dev` to set up a local dev environment.
3. Ensure `uv run --project server pytest server/tests -q` passes for any code change.
4. Run `uv run --project server ruff check server` before submitting.
5. Submit a pull request against `main` with a clear description of what and why.

The project is licensed under Apache 2.0. By contributing you agree that your contributions will be licensed under the same terms.