/**
 * Client for RoboPay's ownership-gated mobile API (car/mobile_ownership.py
 * + the /mobile/* routes in car/seller_app.py). No password/API key lives
 * here on purpose - access is proven per-request by signing a short-lived
 * challenge with the connected wallet, so there is nothing secret for a
 * decompiled APK to leak.
 */

export const ROBOPAY_BASE_URL = 'https://robopay.staexhosting.com/seller';

export type WalletInfo = {
  address: string;
  balance: number | null;
  symbol: string;
  network: string;
};

export type RoboPayStatus = {
  solana: WalletInfo;
  peaq: WalletInfo;
  nft: {machine_id: string; network: string};
  sensors: {distance_cm: number | null; snapshot_available: boolean};
};

export class OwnershipDeniedError extends Error {}

export async function fetchChallenge(): Promise<string> {
  const res = await fetch(`${ROBOPAY_BASE_URL}/mobile/challenge`);
  if (!res.ok) {
    throw new Error(`Could not fetch challenge (HTTP ${res.status})`);
  }
  const data = await res.json();
  return data.message as string;
}

/**
 * signatureBase58 must be the base58-encoded Ed25519 signature the
 * connected wallet produced over `message`'s UTF-8 bytes (see
 * useRoboPayOwnership's call to wallet.signMessages()).
 */
export async function verifyOwnership(
  pubkey: string,
  message: string,
  signatureBase58: string,
): Promise<RoboPayStatus> {
  const res = await fetch(`${ROBOPAY_BASE_URL}/mobile/verify`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pubkey, message, signature: signatureBase58}),
  });
  if (res.status === 403) {
    const body = await res.json().catch(() => ({error: 'access denied'}));
    throw new OwnershipDeniedError(body.error ?? 'This wallet does not own the machine');
  }
  if (!res.ok) {
    throw new Error(`Verification failed (HTTP ${res.status})`);
  }
  return (await res.json()) as RoboPayStatus;
}

export function snapshotUrl(): string {
  // Cache-busted so <Image> actually refetches on every poll instead of
  // reusing a cached frame.
  return `${ROBOPAY_BASE_URL}/mobile/snapshot.jpg?t=${Date.now()}`;
}
