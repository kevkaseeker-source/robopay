import * as anchor from "@coral-xyz/anchor";
import { Program } from "@coral-xyz/anchor";
import { Keypair, PublicKey, SystemProgram, LAMPORTS_PER_SOL } from "@solana/web3.js";
import { assert } from "chai";
import { MachineSplit } from "../target/types/machine_split";

describe("machine_split", () => {
  const provider = anchor.AnchorProvider.env();
  anchor.setProvider(provider);
  const program = anchor.workspace.MachineSplit as Program<MachineSplit>;
  const authority = (provider.wallet as anchor.Wallet).payer;
  const owners = [Keypair.generate(), Keypair.generate(), Keypair.generate()];
  const machineId = Array.from(Keypair.generate().publicKey.toBytes());
  const [split] = PublicKey.findProgramAddressSync(
    [Buffer.from("split"), Buffer.from(machineId)], program.programId,
  );

  it("initializes, receives 0.12 SOL, and distributes by shares", async () => {
    await program.methods.initSplit(machineId, owners.map((k) => k.publicKey), [5000, 2500, 2500])
      .accounts({ split, authority: authority.publicKey, systemProgram: SystemProgram.programId }).rpc();

    const before = await Promise.all(owners.map((k) => provider.connection.getBalance(k.publicKey)));
    await program.methods.payDelivery(new anchor.BN(0.12 * LAMPORTS_PER_SOL))
      .accounts({ split, payer: authority.publicKey, systemProgram: SystemProgram.programId }).rpc();
    await program.methods.distribute().accounts({ split })
      .remainingAccounts(owners.map((k) => ({ pubkey: k.publicKey, isSigner: false, isWritable: true }))).rpc();
    const after = await Promise.all(owners.map((k) => provider.connection.getBalance(k.publicKey)));
    assert.deepEqual(after.map((n, i) => n - before[i]), [60_000_000, 30_000_000, 30_000_000]);
  });

  it("rejects a 9000 bps share total", async () => {
    const badMachine = Array.from(Keypair.generate().publicKey.toBytes());
    const [badSplit] = PublicKey.findProgramAddressSync([Buffer.from("split"), Buffer.from(badMachine)], program.programId);
    try {
      await program.methods.initSplit(badMachine, owners.map((k) => k.publicKey), [5000, 2000, 2000])
        .accounts({ split: badSplit, authority: authority.publicKey, systemProgram: SystemProgram.programId }).rpc();
      assert.fail("expected invalid share total");
    } catch (error) {
      assert.include(String(error), "InvalidShareTotal");
    }
  });

  it("rejects recipients in the wrong owner order", async () => {
    try {
      await program.methods.distribute().accounts({ split })
        .remainingAccounts([owners[1], owners[0], owners[2]].map((k) => ({ pubkey: k.publicKey, isSigner: false, isWritable: true }))).rpc();
      assert.fail("expected owner order mismatch");
    } catch (error) {
      assert.include(String(error), "OwnerAccountsMismatch");
    }
  });
});
