# Security Policy

Liquidity Spot is experimental P2P coordination software. Please be careful with security reports and avoid posting sensitive details in public issues.

## Reporting A Vulnerability

If you find a security issue, please report it privately to the maintainers before opening a public issue.

Include:

- affected route, page, or feature;
- steps to reproduce;
- expected impact;
- whether any secret, token, session, wallet data, or trade data may be exposed;
- suggested fix, if known.

Do not include real private keys, seed phrases, wallet passwords, API keys, or user credentials in reports.

## In Scope

- Authentication and session handling.
- GFAVIP callback/token handling.
- Gems wallet API usage.
- P2P trade-room authorization.
- Trade state transitions.
- Secret/hash handling in experimental swap flows.
- Admin access controls.
- Accidental exposure of environment variables, local databases, logs, or private specs.

## Out Of Scope

- Social engineering between P2P traders.
- Losses caused by users sending funds to the wrong address.
- Bugs in external wallets, block explorers, exchanges, or GFAVIP services.
- Denial-of-service against free/community deployments, unless it exposes data.

## Secrets

Never commit:

- `.env`;
- local databases;
- API keys;
- bearer tokens;
- deploy logs;
- private planning specs;
- seed phrases or wallet credentials.

If a secret is ever committed, remove it from Git history and rotate it immediately.
