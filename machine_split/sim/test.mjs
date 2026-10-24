import assert from 'node:assert/strict';

export function validateShares(owners, bps) {
  if (!Array.isArray(owners) || owners.length < 1 || owners.length > 8) throw new Error('owner count');
  if (!Array.isArray(bps) || owners.length !== bps.length) throw new Error('length');
  if (owners.some((x) => !x) || new Set(owners).size !== owners.length) throw new Error('owner');
  if (bps.some((x) => !Number.isInteger(x) || x < 0 || x > 65535) || bps.reduce((a, b) => a + b, 0) !== 10000) throw new Error('bps');
}

export function splitAmount(amount, bps) {
  if (!Number.isSafeInteger(amount) || amount < 0) throw new Error('amount');
  validateShares(bps.map((_, i) => `owner-${i}`), bps);
  let assigned = 0;
  return bps.map((share, i) => {
    if (i === bps.length - 1) return amount - assigned;
    const part = Math.floor((amount * share) / 10000);
    assigned += part;
    return part;
  });
}

assert.deepEqual(splitAmount(120_000_000, [5000, 2500, 2500]), [60_000_000, 30_000_000, 30_000_000]);
assert.deepEqual(splitAmount(1, [5000, 2500, 2500]), [0, 0, 1]);
assert.equal(splitAmount(101, [5000, 2500, 2500]).reduce((a, b) => a + b, 0), 101);
for (const [owners, shares] of [[[], []], [['a'], [9000]], [['a', 'b'], [5000]], [['a', 'a'], [5000, 5000]], [Array.from({ length: 9 }, (_, i) => `${i}`), Array(9).fill(10000 / 9)]]) {
  assert.throws(() => validateShares(owners, shares));
}
assert.throws(() => splitAmount(-1, [10000]));
console.log('All machine-split math and validation assertions passed.');
