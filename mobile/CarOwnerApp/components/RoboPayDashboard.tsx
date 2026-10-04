import React, {useCallback, useState} from 'react';
import {
  ActivityIndicator,
  Button,
  Image,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import {
  transact,
  Web3MobileWallet,
} from '@solana-mobile/mobile-wallet-adapter-protocol-web3js';
import bs58 from 'bs58';

import {useAuthorization, Account} from './providers/AuthorizationProvider';
import NftTransferCard from './NftTransferCard';
import {
  fetchChallenge,
  verifyOwnership,
  snapshotUrl,
  OwnershipDeniedError,
  RoboPayStatus,
} from '../services/robopayApi';

type Status = 'idle' | 'checking' | 'owner' | 'not_owner' | 'error';

type RoboPayDashboardProps = Readonly<{
  selectedAccount: Account;
}>;

export default function RoboPayDashboard({
  selectedAccount,
}: RoboPayDashboardProps) {
  const {authorizeSession} = useAuthorization();
  const [status, setStatus] = useState<Status>('idle');
  const [data, setData] = useState<RoboPayStatus | null>(null);
  const [errorMessage, setErrorMessage] = useState<string>('');
  const [snapshotKey, setSnapshotKey] = useState(0);

  const checkOwnership = useCallback(async () => {
    setStatus('checking');
    setErrorMessage('');
    try {
      const message = await fetchChallenge();

      const signatureBytes = await transact(async (wallet: Web3MobileWallet) => {
        const authResult = await authorizeSession(wallet);
        const messageBuffer = new Uint8Array(
          message.split('').map(c => c.charCodeAt(0)),
        );
        const signed = await wallet.signMessages({
          addresses: [authResult.address],
          payloads: [messageBuffer],
        });
        return signed[0];
      });

      const result = await verifyOwnership(
        selectedAccount.publicKey.toBase58(),
        message,
        bs58.encode(signatureBytes),
      );
      setData(result);
      setStatus('owner');
      setSnapshotKey(k => k + 1);
    } catch (err) {
      if (err instanceof OwnershipDeniedError) {
        setErrorMessage(err.message);
        setStatus('not_owner');
      } else {
        setErrorMessage(err instanceof Error ? err.message : String(err));
        setStatus('error');
      }
    }
  }, [authorizeSession, selectedAccount.address]);

  return (
    <View style={styles.container}>
      <Text style={styles.header}>PiCarX — Machine Dashboard</Text>

      {status === 'idle' && (
        <Button title="Check machine ownership" onPress={checkOwnership} />
      )}

      {status === 'checking' && (
        <View style={styles.center}>
          <ActivityIndicator />
          <Text>Sign the request in your wallet to prove ownership…</Text>
        </View>
      )}

      {status === 'not_owner' && (
        <View style={styles.center}>
          <Text style={styles.deniedText}>
            This wallet does not currently own the PiCarX Machine-NFT.
          </Text>
          <Text style={styles.subtleText}>{errorMessage}</Text>
          <Button title="Try again" onPress={checkOwnership} />
        </View>
      )}

      {status === 'error' && (
        <View style={styles.center}>
          <Text style={styles.deniedText}>Something went wrong.</Text>
          <Text style={styles.subtleText}>{errorMessage}</Text>
          <Button title="Retry" onPress={checkOwnership} />
        </View>
      )}

      {status === 'owner' && data && (
        <View>
          <Text style={styles.ownerBadge}>✓ Verified machine owner</Text>

          <View style={styles.row}>
            <Text style={styles.chainLabel}>Solana ({data.solana.network})</Text>
            <Text style={styles.amount}>
              {data.solana.balance ?? '—'} {data.solana.symbol}
            </Text>
          </View>

          <View style={styles.row}>
            <Text style={styles.chainLabel}>peaq ({data.peaq.network})</Text>
            <Text style={styles.amount}>
              {data.peaq.balance ?? '—'} {data.peaq.symbol}
            </Text>
          </View>

          <View style={styles.row}>
            <Text style={styles.chainLabel}>Machine-NFT</Text>
            <Text style={styles.nftId} numberOfLines={1}>
              {data.nft.machine_id}
            </Text>
          </View>

          <View style={styles.row}>
            <Text style={styles.chainLabel}>Ultrasonic distance</Text>
            <Text style={styles.amount}>
              {data.sensors.distance_cm ?? '—'} cm
            </Text>
          </View>

          {data.sensors.snapshot_available && (
            <Image
              key={snapshotKey}
              style={styles.snapshot}
              source={{uri: snapshotUrl()}}
              resizeMode="cover"
            />
          )}

          <Button title="Refresh" onPress={checkOwnership} />

          <NftTransferCard machineId={data.nft.machine_id} />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    padding: 16,
  },
  header: {
    fontSize: 18,
    fontWeight: 'bold',
    marginBottom: 12,
  },
  center: {
    alignItems: 'center',
    gap: 8,
  },
  deniedText: {
    fontWeight: '600',
    color: '#b91c1c',
  },
  subtleText: {
    fontSize: 12,
    color: '#666',
    marginBottom: 8,
  },
  ownerBadge: {
    color: '#15803d',
    fontWeight: '600',
    marginBottom: 12,
  },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: 6,
    borderBottomWidth: 1,
    borderBottomColor: '#eee',
  },
  chainLabel: {
    color: '#555',
  },
  amount: {
    fontWeight: '600',
  },
  nftId: {
    fontWeight: '600',
    maxWidth: 180,
  },
  snapshot: {
    width: '100%',
    height: 200,
    marginTop: 12,
    borderRadius: 8,
    backgroundColor: '#eee',
  },
});
