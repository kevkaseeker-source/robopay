import React, {useCallback, useState} from 'react';
import {
  ActivityIndicator,
  Button,
  Linking,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import {useEvmWallet} from './providers/EvmWalletProvider';
import {buildTransferMachineTx, InvalidAddressError} from '../services/peaqTransfer';

type TransferStatus = 'idle' | 'sending' | 'sent' | 'error';

type NftTransferCardProps = Readonly<{
  machineId: string;
}>;

export default function NftTransferCard({machineId}: NftTransferCardProps) {
  const {address, connectUri, connecting, connect, disconnect, sendTransaction} =
    useEvmWallet();
  const [recipient, setRecipient] = useState('');
  const [status, setStatus] = useState<TransferStatus>('idle');
  const [message, setMessage] = useState('');

  const handleTransfer = useCallback(async () => {
    if (!address) {
      return;
    }
    setStatus('sending');
    setMessage('');
    try {
      const tx = buildTransferMachineTx(address, recipient.trim(), machineId);
      const txHash = await sendTransaction(tx);
      setStatus('sent');
      setMessage(`Transfer submitted: ${txHash}`);
    } catch (err) {
      setStatus('error');
      if (err instanceof InvalidAddressError) {
        setMessage(err.message);
      } else {
        setMessage(err instanceof Error ? err.message : String(err));
      }
    }
  }, [address, recipient, machineId, sendTransaction]);

  return (
    <View style={styles.container}>
      <Text style={styles.header}>Transfer Machine Ownership</Text>
      <Text style={styles.warning}>
        This moves real ownership of the machine on peaq mainnet. The new
        owner will need to claim DID control separately before payouts
        route to them.
      </Text>

      {!address ? (
        <>
          <Button
            title={connecting ? 'Connecting…' : 'Connect EVM Wallet'}
            onPress={connect}
            disabled={connecting}
          />
          {connectUri && (
            <View style={styles.qrContainer}>
              <Text style={styles.subtleText}>
                Opens your EVM wallet app (e.g. MetaMask) to approve the connection.
              </Text>
              <Button
                title="Open in Wallet"
                onPress={() => Linking.openURL(connectUri)}
              />
            </View>
          )}
        </>
      ) : (
        <>
          <Text style={styles.subtleText}>Connected: {address}</Text>
          <Button title="Disconnect" onPress={disconnect} color="#b91c1c" />

          <Text style={[styles.subtleText, styles.fieldLabel]}>
            New owner's EVM address
          </Text>
          <TextInput
            style={styles.input}
            value={recipient}
            onChangeText={setRecipient}
            placeholder="0x..."
            autoCapitalize="none"
            autoCorrect={false}
          />

          <Button
            title={status === 'sending' ? 'Sending…' : 'Transfer Ownership'}
            onPress={handleTransfer}
            disabled={status === 'sending' || recipient.trim().length === 0}
          />

          {status === 'sending' && <ActivityIndicator style={styles.spinner} />}
          {status === 'sent' && <Text style={styles.successText}>{message}</Text>}
          {status === 'error' && <Text style={styles.errorText}>{message}</Text>}
        </>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    padding: 16,
  },
  header: {
    fontSize: 16,
    fontWeight: 'bold',
    marginBottom: 6,
  },
  warning: {
    fontSize: 12,
    color: '#92400e',
    marginBottom: 12,
  },
  subtleText: {
    fontSize: 12,
    color: '#666',
    marginVertical: 6,
  },
  fieldLabel: {
    marginTop: 12,
  },
  input: {
    borderWidth: 1,
    borderColor: '#ccc',
    borderRadius: 6,
    padding: 10,
    marginBottom: 12,
    fontFamily: 'monospace',
  },
  qrContainer: {
    alignItems: 'center',
    marginTop: 16,
    gap: 8,
  },
  spinner: {
    marginTop: 10,
  },
  successText: {
    color: '#15803d',
    marginTop: 10,
  },
  errorText: {
    color: '#b91c1c',
    marginTop: 10,
  },
});
