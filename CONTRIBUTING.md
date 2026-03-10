# Contributing

## Development Setup

1. Install Python 3.12+ and Docker.
2. Copy `.env.example` to `.env` and fill in your own credentials and endpoints.
3. Run `./setup.sh` for the guided setup path, or `docker compose up -d` if you already know the required environment.

## Validation

Run the test suite before opening a pull request:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --no-sync pytest -q
```

If you change documentation-only files, call that out clearly in the pull request.

## Pull Requests

- Keep changes scoped.
- Prefer typed contracts and explicit boundaries over hidden cross-layer access.
- Do not commit secrets, tokens, passwords, `.env`, or private deployment helpers.
- Add or update tests when changing runtime behavior.

## Commit Style

Short, imperative commit messages are preferred, for example:

- `Refactor writer review loop`
- `Reject invalid published_at values`
- `Refresh README for service architecture`
