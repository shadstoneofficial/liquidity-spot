---
name: "agentic-swaps"
description: "Guides AI agents (like Janice) on how to interact with Liquidity.spot to perform decentralized BTC, HNS, and USD swaps without custodial risk. Invoke when agent needs to execute a swap."
---

# Agentic Swaps on Liquidity.spot

This skill provides the standard operating procedure for AI agents to execute decentralized, non-custodial swaps on the Liquidity.spot platform.

## 1. Core Principles
- **Non-Custodial:** Liquidity.spot NEVER holds funds. All swaps must use HTLCs (Hash Time Locked Contracts) or direct settlement with slashed collateral.
- **API First:** Agents should interact with the Liquidity.spot REST API (currently in development) to read orderbooks and post trade messages.
- **Headless Communication:** Do not dump large text in Slack/Discord. Route all platform communication through the centralized inbox (`POST /api/issues`) using headless SSO.

## 2. BTC <-> HNS Atomic Swaps (Agentic Execution)
Agents can fully automate the existing Atomic Swap protocol:
1. **Discover:** Fetch the open orderbook via `GET /api/v1/offers`.
2. **Match:** Accept an order to create a P2P Trade Room.
3. **Lock:** Construct the HTLC on the origin chain (e.g., Bitcoin) using your local node. Share the Transaction ID and Hash Lock in the Trade Room via structured JSON message.
4. **Verify:** Monitor the destination chain (e.g., Handshake) for the counterparty's HTLC using the exact same Hash Lock.
5. **Claim:** Reveal the secret to claim the funds on the destination chain. The counterparty will observe the blockchain, extract the revealed secret, and claim their funds on the origin chain.

## 3. USD (MPP) <-> Crypto Swaps
Since fiat USD cannot be natively locked in an HTLC, we rely on **Agentic Escrow** via GFA Gems:
1. **Bonding:** As a Market Maker Agent, you must lock GFA Gems as a collateral bond on Liquidity.spot when creating your offer.
2. **Payment:** The buyer sends USD via MPP (Multi-Party Protocol) rails directly to your designated receiving account.
3. **Verification:** You must programmatically verify the MPP payment receipt.
4. **Release:** Once verified, broadcast the transaction to send BTC/HNS directly to the buyer's wallet.
5. **Penalty (Slashing):** If you receive the USD but fail to release the crypto, the human buyer will dispute the trade. The platform Admin will verify the MPP receipt and slash your locked GFA Gems.

## 4. Required Capabilities for the Agent
To perform these tasks, the agent MUST have access to:
- A Bitcoin node or light client capable of constructing PSBTs and HTLC scripts.
- A Handshake node or light client capable of constructing HTLC scripts.
- API access to the MPP protocol to verify incoming USD receipts.
