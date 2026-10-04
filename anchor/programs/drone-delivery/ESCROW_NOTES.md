# Drohnen-Lieferungs-Escrow — Recherche & Plan
_Zuletzt aktualisiert: 2026-05-12_

---

## Ziel

Käufer zahlt `delivery_fee + product_price` in einen PDA-Vault.
Drohne erreicht GPS-Koordinaten → on-chain bestätigt → Split-Payment:
- `delivery_fee` → Kevin (drone_operator)
- `product_price` → Seller
- Bei Timeout oder Abbruch → alles zurück an Buyer

---

## Account-Struktur (geplant)

```rust
pub struct DeliveryEscrow {
    pub buyer: Pubkey,
    pub seller: Pubkey,
    pub drone_operator: Pubkey,   // Kevin
    pub delivery_fee: u64,
    pub product_price: u64,
    pub target_lat: i64,          // Grad × 1_000_000_0
    pub target_lon: i64,
    pub deadline: i64,            // Unix timestamp
    pub status: DeliveryStatus,   // Pending / Delivered / Cancelled
}
```

---

## Instructions (geplant)

### 1. `create_delivery(delivery_fee, product_price, target_lat, target_lon, deadline)`
- Aufrufer: Buyer
- Buyer zahlt `delivery_fee + product_price` → PDA-Vault (lamports) via CPI
- Status → Pending

```rust
// SOL in PDA einzahlen (CPI an System Program)
let cpi_context = CpiContext::new(
    ctx.accounts.system_program.to_account_info(),
    system_program::Transfer {
        from: ctx.accounts.buyer.to_account_info(),
        to: ctx.accounts.escrow_vault.to_account_info(),
    },
);
system_program::transfer(cpi_context, delivery_fee + product_price)?;
```

### 2. `confirm_delivery(actual_lat, actual_lon, timestamp)`
- Aufrufer: drone_operator (Kevin / RPi4-Wallet)
- GPS-Check: `|actual - target| < Toleranz`
- Deadline-Check: `current_time <= deadline`
- Split-Payment via `try_borrow_mut_lamports()`:

```rust
// Split-Payment direkt aus PDA-Vault
**ctx.accounts.escrow_vault.try_borrow_mut_lamports()? -= delivery_fee;
**ctx.accounts.drone_operator.try_borrow_mut_lamports()? += delivery_fee;

**ctx.accounts.escrow_vault.try_borrow_mut_lamports()? -= product_price;
**ctx.accounts.seller.try_borrow_mut_lamports()? += product_price;
```

### 3. `cancel_delivery()`
- Aufrufer: Buyer
- Nur wenn `current_time > deadline` (Time-Gate wie Voting-Programm)
- Alles zurück an Buyer

---

## Kritische Code-Patterns (aus Recherche)

### SOL in PDA einzahlen
```rust
// system_program::transfer CPI
// Quelle: solana.com/developers/guides/games/store-sol-in-pda
```

### SOL aus PDA auszahlen (ohne Signer nötig — PDA kann selbst "signen")
```rust
// try_borrow_mut_lamports() — direkte Lamport-Manipulation
// Funktioniert weil PDAs kein Private Key haben und vom Programm kontrolliert werden
```

### Deadline / Timeout (aus Voting-Programm Pattern)
```rust
let clock = Clock::get()?;
if clock.unix_timestamp > escrow.deadline {
    return Err(ErrorCode::DeliveryExpired.into());
}
```

### invoke_signed (Native Rust, falls kein Anchor)
```rust
invoke_signed(&transfer_ix, &accounts, &[&[b"escrow", &[bump]]])?;
```

---

## Referenz-Quellen (vollständig)

| Quelle | Was ist nützlich |
|---|---|
| [anchor-escrow-2026 (solanakite)](https://github.com/solanakite/anchor-escrow-2026) | Aktuellstes Escrow-Repo 2026, clean build, Lernressource |
| [anchor-escrow-2025 (mikemaccana)](https://github.com/mikemaccana/anchor-escrow-2025) | Gut dokumentiert, Video-Tutorial dazu |
| [YouTube Tutorial (Mike MacCana)](https://www.youtube.com/watch?v=x7OoYpoWAVM) | Vollständiger Walkthrough Escrow mit Anchor |
| [Storing SOL in a PDA (solana.com)](https://solana.com/developers/guides/games/store-sol-in-pda) | **Wichtigste Quelle für Kevin** — lamports in PDA + split-payment Code |
| [solana-program/escrow (official)](https://github.com/solana-program/escrow) | Offizielles konfigurierbares Escrow-Programm |
| [paulx.dev Escrow Einführung](https://paulx.dev/blog/2021/01/14/programming-on-solana-an-introduction/) | Klassiker — PDA-Vault + invoke_signed erklärt |
| [HackMD Anchor Escrow](https://hackmd.io/@ironaddicteddog/anchor_example_escrow) | Anchor-spezifische Erklärung |
| [Quiknode Escrow 2025](https://github.com/quiknode-labs/you-will-build-a-solana-program) | Vault-Pattern: SOL sperren + Refund-Logik |
| [Solana Bootcamp Escrow](https://solana.com/de/developers/bootcamp/program-patterns/escrow-application) | State-Transitions + Authority-Validation |
| [Bootcamp Voting 03](https://github.com/solana-foundation/solana-bootcamp-2026/tree/main/03-voting) | Time-Gating Pattern (Deadline / Timeout) |

---

## Aktueller Stand

- **Program ID (Devnet):** `3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y`
- `initialize_delivery()` + `confirm_delivery()` bereits deployed
- Lokal: `src/lib.rs` (dieser Ordner)
- **Fehlt noch:** Vault (SOL einzahlen), Split-Payment, Deadline

---

## Nächste Schritte für lib.rs Umbau

1. `system_program::transfer` CPI → SOL in PDA-Vault bei `create_delivery`
2. GPS-Toleranz-Check in `confirm_delivery`
3. Deadline-Check (`Clock::get()`)
4. Split via `try_borrow_mut_lamports()`: fee → operator, price → seller
5. `cancel_delivery` mit Timeout-Guard
