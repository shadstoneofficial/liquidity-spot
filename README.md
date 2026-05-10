# Liquidity Spot

Liquidity Spot is an open-source P2P coordination app for HNS/BTC-style liquidity trades.

The current product direction is human P2P first: users can browse offers, create offers, accept offers, and coordinate non-custodial trades without the platform taking custody of funds. Experimental trustless/atomic-swap screens remain in the codebase for learning and testing, but the primary flow is guided P2P coordination.

## Status

This project is early and should be treated as experimental software.

- Public browsing and normal P2P actions are designed to work in guest mode.
- GFAVIP login is optional for normal P2P use.
- GFAVIP is only required for features that depend on a GFAVIP account, such as Gems or account-linked benefits.
- The app does not custody user funds.
- Users are responsible for verifying counterparties, addresses, transaction IDs, and settlement before releasing any asset.

## Features

- Public P2P offer browsing.
- Guest-mode offer creation and acceptance.
- Trade rooms for buyer/seller coordination.
- Optional GFAVIP login for Gems and account-linked benefits.
- Optional maker Gems bond flow when GFAVIP wallet API credentials are configured.
- Experimental HTLC/atomic-swap learning screens.
- Admin review surfaces for trades and disputes.

## Local Development

Requirements:

- Python 3.11+
- SQLite for local development
- PostgreSQL for production-style deployments

Setup:

```bash
git clone https://github.com/shadstoneofficial/liquidity-spot.git
cd liquidity-spot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Open:

```txt
http://localhost:8000
```

The app creates local database tables on startup.

## Environment Variables

Copy `.env.example` to `.env` for local development.

```txt
FLASK_ENV=development
SECRET_KEY=change-me-local-dev-only
DATABASE_URL=sqlite:///app.db
GFAVIP_SERVICE_NAME=liquidity-spot
REDIRECT_URI=http://localhost:8000/callback
GFAVIP_WALLET_API_KEY=
GFAVIP_WALLET_BASE_URL=https://wallet.gfavip.com
```

Production notes:

- Always set a strong `SECRET_KEY`.
- Use a production database through `DATABASE_URL`.
- Leave `GFAVIP_WALLET_API_KEY` empty unless Gems wallet integration is intentionally enabled.
- Do not commit `.env`, local databases, deploy logs, or private specs.

## GFAVIP Policy

Liquidity Spot should not require GFAVIP login for basic P2P usage.

Guest users can:

- browse public offers;
- create offers;
- accept offers;
- enter trade rooms;
- coordinate trades manually.

GFAVIP login adds:

- Gems-backed features;
- account-linked history;
- future reputation and notification features;
- wallet-service integration.

## Safety Model

Liquidity Spot is a coordination layer, not a custodian.

The app should never ask for:

- seed phrases;
- private keys;
- wallet passwords;
- remote desktop access;
- irreversible transfers before the user independently verifies terms.

For now, users should treat all trades as manual P2P coordination and verify every step out of band.

## Bob Wallet Addon Direction

Liquidity Spot is intended to become the first external Bob Wallet Add On candidate after Shakedex Marketplace.

Initial integration should be conservative:

- open externally or in a constrained embedded view;
- no automatic wallet signing;
- no seed/private-key access;
- explicit user prompts for any future wallet-assisted action.

The draft Bob Add On manifest is available at:

```txt
https://liquidity.spot/bob-addon.json
```

See the hub planning docs in `hub-learnhns/temp-specs` for the broader Bob Add Ons roadmap.

## Deployment

The repo includes a Dockerfile and Railway config.

```bash
docker build -t liquidity-spot .
docker run --env-file .env -p 8000:8000 liquidity-spot
```

Railway deploys use:

```txt
./start.sh
```

## Security

Please do not open public issues for security-sensitive findings. See `SECURITY.md`.

## License

MIT. See `LICENSE`.
