# Contributing

Thanks for helping Liquidity Spot.

The project is still early, so the most useful contributions are small, easy to review, and focused on safety.

## Development Setup

Liquidity Spot supports Python 3.11, matching the Docker image and CI. Create
an isolated environment; do not install its packages globally.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
cp .env.example .env
python run.py
```

The application uses SQLite locally by default. The `.env` file and local
database files are ignored by Git.

## Tests

Tests use the explicit `testing` configuration: an in-memory SQLite database,
a test-only secret, the `regtest` network, no wallet credential or chain
watcher, and an application-level block on outbound HTTP. You do not need to
create `.env` to run them.

Run the complete suite with one command from the repository root:

```bash
python -m unittest discover -s tests -v
```

Run the same compile check used by CI before opening a pull request:

```bash
python -m compileall app.py config.py models.py routes services
```

`requirements.txt` is the short, human-maintained list of direct production
dependencies. `requirements-lock.txt` pins the complete Python 3.11 dependency
graph used locally and in CI. To intentionally update the lock, create and
activate a clean Python 3.11 virtual environment, then run:

```bash
python -m pip install --upgrade -r requirements.txt
python -m pip freeze > requirements-lock.txt
python -m pip check
python -m unittest discover -s tests -v
```

Review all version changes before committing them.

## Contribution Priorities

Good first areas:

- docs and setup improvements;
- guest-mode P2P UX;
- safer trade-room copy and warnings;
- tests around P2P offer/trade state transitions;
- Bob Wallet Add On manifest planning;
- security hardening.

Please avoid large rewrites unless there is already an issue or spec explaining the change.

## Product Principles

- GFAVIP login is optional for normal P2P use.
- Gems and account-linked benefits may require GFAVIP.
- The app should not custody funds.
- Never ask users for seeds, private keys, wallet passwords, or remote access.
- Keep experimental atomic-swap functionality clearly labeled.

## Pull Request Checklist

- No secrets or local data are committed.
- `.env`, databases, logs, and `temp-specs/` stay ignored.
- User-facing text does not imply the platform custodies funds.
- Guest-mode behavior remains available for normal P2P flows.
- Relevant Python files compile.
