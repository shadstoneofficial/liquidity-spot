# Contributing

Thanks for helping Liquidity Spot.

The project is still early, so the most useful contributions are small, easy to review, and focused on safety.

## Development Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Run a syntax check before opening a pull request:

```bash
python3 -m compileall app.py config.py models.py routes services
```

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
