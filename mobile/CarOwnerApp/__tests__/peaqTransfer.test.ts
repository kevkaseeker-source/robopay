import {buildTransferMachineTx, InvalidAddressError, MACHINE_REGISTRY_ADDRESS} from '../services/peaqTransfer';

const OWNER = '0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f';
const NEW_OWNER = '0x0ED93a294161E18B5d7e6102B5a21382FDeBbb7E';
const MACHINE_ID = '5149596011477982620423871887556457753159696362422273317835964893685329625592';

test('builds a correctly-targeted transferFrom call', () => {
  const tx = buildTransferMachineTx(OWNER, NEW_OWNER, MACHINE_ID);
  expect(tx.to).toBe(MACHINE_REGISTRY_ADDRESS);
  expect(tx.from.toLowerCase()).toBe(OWNER.toLowerCase());
  // transferFrom(address,address,uint256) selector
  expect(tx.data.startsWith('0x23b872dd')).toBe(true);
});

test('encodes the machine id as the exact tokenId in calldata', () => {
  const tx = buildTransferMachineTx(OWNER, NEW_OWNER, MACHINE_ID);
  const tokenIdHex = BigInt(MACHINE_ID).toString(16).padStart(64, '0');
  expect(tx.data.toLowerCase()).toContain(tokenIdHex);
});

test('rejects a malformed fromAddress', () => {
  expect(() => buildTransferMachineTx('not-an-address', NEW_OWNER, MACHINE_ID)).toThrow(
    InvalidAddressError,
  );
});

test('rejects a malformed toAddress', () => {
  expect(() => buildTransferMachineTx(OWNER, 'not-an-address', MACHINE_ID)).toThrow(
    InvalidAddressError,
  );
});

test('refuses a transfer to the same address (no-op)', () => {
  expect(() => buildTransferMachineTx(OWNER, OWNER, MACHINE_ID)).toThrow(InvalidAddressError);
});

test('refuses a non-numeric machineId', () => {
  expect(() => buildTransferMachineTx(OWNER, NEW_OWNER, 'not-a-number')).toThrow(
    InvalidAddressError,
  );
});

test('checksums mixed-case addresses consistently', () => {
  const lower = buildTransferMachineTx(OWNER.toLowerCase(), NEW_OWNER.toLowerCase(), MACHINE_ID);
  const mixed = buildTransferMachineTx(OWNER, NEW_OWNER, MACHINE_ID);
  expect(lower.from).toBe(mixed.from);
  expect(lower.data).toBe(mixed.data);
});
