1. The robot's machine owner initializes the split PDA with its ordered owners and basis points.
2. Configure the RoboPay listing's seller/settlement destination as that split PDA.
3. Keep the PDA address and machine ID in the escrow record so release can verify the destination.
4. After delivery confirmation, the escrow program releases exactly the agreed lamports to the PDA.
5. If escrow is a separate program, it can invoke `pay_delivery` with its signing authority as payer.
6. The split PDA must be passed as the `split` account and the System Program as `system_program`.
7. Alternatively, escrow may transfer lamports directly; the split then accounts for funds on distribution.
8. Anyone can call `distribute`, passing writable recipient accounts in the configured owner order.
9. Distribution preserves the PDA rent floor and gives rounding remainder to the final owner.
10. Share changes require the recorded authority and an empty distributable vault.
