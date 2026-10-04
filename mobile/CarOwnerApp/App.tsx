import {
  ConnectionProvider,
  RPC_ENDPOINT,
} from './components/providers/ConnectionProvider';
import {clusterApiUrl} from '@solana/web3.js';
import React from 'react';
import {SafeAreaView, StyleSheet} from 'react-native';
import {AuthorizationProvider} from './components/providers/AuthorizationProvider';
import {EvmWalletProvider} from './components/providers/EvmWalletProvider';
import {Header} from './components/Header';

import MainScreen from './screens/MainScreen';

export default function App() {
  return (
    <ConnectionProvider
      config={{commitment: 'processed'}}
      endpoint={clusterApiUrl(RPC_ENDPOINT)}>
      <AuthorizationProvider>
        <EvmWalletProvider>
          <SafeAreaView style={styles.shell}>
            <Header />
            <MainScreen />
          </SafeAreaView>
        </EvmWalletProvider>
      </AuthorizationProvider>
    </ConnectionProvider>
  );
}

const styles = StyleSheet.create({
  shell: {
    height: '100%',
  },
});
