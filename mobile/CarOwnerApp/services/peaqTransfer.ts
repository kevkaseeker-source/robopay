/**
 * ERC-721 machine-ownership transfer on peaq mainnet, built from scratch
 * against the standard transferFrom(address,address,uint256) interface -
 * confirmed by reading peaq_os_sdk's own machine_transfer.py source
 * (robopay-research-group repo, 2026-10-02): the SDK's transfer_machine()
 * ultimately calls this exact standard ERC-721 method on MachineRegistry,
 * nothing peaq-custom. That means any standard EVM wallet (MetaMask,
 * Rainbow, etc. via WalletConnect) can sign this without any peaq-specific
 * ABI support - we only need the four-byte selector and standard encoding.
 *
 * Deliberately does NOT use peaq_os_sdk's richer preflight checks
 * (controllerOf/getApproved/isApprovedForAll/isRelocating reads) - those
 * require a server-held key to run server-side, which is exactly what this
 * app avoids. The on-chain transferFrom call enforces real ownership
 * itself (reverts if the connected wallet isn't the owner/approved), so a
 * malicious or mistaken call fails safely on-chain either way. We only
 * duplicate the one check that matters for UX: showing the user whether
 * they're even likely to succeed before they pay gas to find out.
 */
import {ethers} from 'ethers';

export const PEAQ_CHAIN_ID = 3338;
export const PEAQ_RPC_URL = 'https://peaq.api.onfinality.io/public';
export const MACHINE_REGISTRY_ADDRESS = '0x64b93Cc29b251fAFa83BD110cDB1C24207f85536';

// Only the one function we actually call - no need to carry the full
// MachineRegistry ABI into the app bundle.
const TRANSFER_FROM_ABI = ['function transferFrom(address from, address to, uint256 tokenId)'];
const erc721Interface = new ethers.Interface(TRANSFER_FROM_ABI);

export class InvalidAddressError extends Error {}

function assertEvmAddress(address: string, field: string): string {
  if (!ethers.isAddress(address)) {
    throw new InvalidAddressError(`${field} is not a valid EVM address: ${address}`);
  }
  return ethers.getAddress(address); // checksummed
}

/**
 * Builds the raw calldata + target address for a machine-ownership
 * transfer. Returns a plain object shaped for eth_sendTransaction's
 * params (WalletConnect/MetaMask expect hex strings), not a signed or
 * submitted transaction - signing happens in the connected wallet via
 * WalletConnect, never here.
 */
export function buildTransferMachineTx(
  fromAddress: string,
  toAddress: string,
  machineId: string,
) {
  const from = assertEvmAddress(fromAddress, 'fromAddress');
  const to = assertEvmAddress(toAddress, 'toAddress');

  if (from.toLowerCase() === to.toLowerCase()) {
    throw new InvalidAddressError('toAddress must differ from fromAddress - refusing a no-op transfer');
  }

  let tokenId: bigint;
  try {
    tokenId = BigInt(machineId);
  } catch (e) {
    throw new InvalidAddressError(`machineId is not a valid uint256: ${machineId}`);
  }
  if (tokenId < 0n) {
    throw new InvalidAddressError(`machineId is not a valid uint256: ${machineId}`);
  }

  const data = erc721Interface.encodeFunctionData('transferFrom', [from, to, tokenId]);

  return {
    from,
    to: MACHINE_REGISTRY_ADDRESS,
    data,
    // Deliberately no gas/gasPrice here - let the connected wallet
    // estimate against current peaq network conditions, same as any
    // normal dApp transaction request.
  };
}
