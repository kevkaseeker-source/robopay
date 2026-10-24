function splitAmount(amount, bps) {
  if (bps.reduce((a, b) => a + b, 0) !== 10000) throw new Error('bps must sum to 10000');
  let assigned = 0;
  return bps.map((share, i) => {
    if (i === bps.length - 1) return amount - assigned;
    const part = Math.floor((amount * share) / 10000);
    assigned += part;
    return part;
  });
}

if (process.argv[2] === 'drone') {
  const names = ['Alice', 'Bob', 'Carla', 'Deniz'];
  const droneBps = [2500, 2500, 2500, 2500];
  const payment = 200_000_000; // 0.20 SOL
  console.log('RoboPay machine-split demo — delivery drone "D-01"');
  console.log('4 co-owners, each holds an NFT for 25 %\n');
  console.log(`Delivery paid: ${(payment / 1e9).toFixed(2)} SOL → split automatically:`);
  splitAmount(payment, droneBps).forEach((v, i) => console.log(`  NFT #${i + 1}  ${names[i].padEnd(6)} 25 %  → ${(v / 1e9).toFixed(3)} SOL`));
  try { splitAmount(payment, [2500, 2500, 2500, 2000]); } catch { console.log('\nRejected: shares 25+25+25+20 % = 95 % (must be 100 %).'); }
  process.exit(0);
}

const owners = ['DHL-like company', 'Alice', 'Bob'];
const bps = [5000, 2500, 2500];
const deliveries = [0.12, 0.25, 0.08].map((sol) => Math.round(sol * 1_000_000_000));
const totals = [0, 0, 0];
console.log('RoboPay machine-split demo — Unit C');
console.log('Co-owners: DHL-like company 50% · Alice 25% · Bob 25%');
console.log('\nDelivery income (SOL) and share income:');
console.log(`${'Delivery'.padEnd(13)} ${'Income'.padStart(10)} ${'Company'.padStart(10)} ${'Alice'.padStart(10)} ${'Bob'.padStart(10)}`);
deliveries.forEach((amount, i) => {
  const parts = splitAmount(amount, bps);
  parts.forEach((value, j) => { totals[j] += value; });
  console.log([`#${i + 1}`.padEnd(13), (amount / 1e9).toFixed(2).padStart(10), ...parts.map((n) => (n / 1e9).toFixed(3).padStart(10))].join(' '));
});
console.log(['Total'.padEnd(13), (deliveries.reduce((a, b) => a + b, 0) / 1e9).toFixed(2).padStart(10), ...totals.map((n) => (n / 1e9).toFixed(3).padStart(10))].join(' '));
console.log('\nRounding edge: 1 lamport at 50/25/25 bps →', splitAmount(1, bps).join(', '), 'lamports (last owner receives remainder).');
try { splitAmount(1, [5000, 2000, 2000]); } catch { console.log('Rejected config: 5000 + 2000 + 2000 = 9000 bps (must equal 10000).'); }
