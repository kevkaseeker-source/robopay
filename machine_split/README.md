# RoboPay machine-split

> Built at the OpenAI DevDay Community Hackathon Berlin (24.10.2026) with Codex. Prototype: compiles (`cargo check`), split math tested (`node sim/test.mjs`), not yet deployed.
Small Solana devnet prototype for people to co-own one delivery robot and share income from deliveries.

## Accounts
- `SplitAccount` PDA seeds are `split` + the 32-byte machine ID; it stores NFT owner authority, ordered owners, bps, bump, and cumulative `total_received`.
- PDA lamports hold the delivery vault; its rent-exempt balance is reserved.
## Instructions
- `init_split(machine_id, owners, bps)`: NFT owner signs as authority; up to 8 unique owners, bps total 10,000.
- `pay_delivery(amount)`: transfers signer lamports into the vault and increments `total_received`.
- `distribute()`: pays spendable balance to writable `remaining_accounts` in owner order; last owner gets rounding remainder.
- `update_shares(owners, bps)`: authority only, when vault contains only its rent floor.
## RoboPay integration
After delivery confirmation, route escrow release to `pay_delivery` on this split PDA instead of a single seller wallet.
Call `distribute` to deliver share income to co-owners. The robot machine NFT owner initializes and updates shares.

Run `node sim/demo.mjs` and `node sim/test.mjs` for the dependency-free demo and math assertions.
