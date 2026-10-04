/**
 * WalletConnect v2 session management for the peaq (EVM) side - separate
 * from AuthorizationProvider.tsx, which only ever speaks Mobile Wallet
 * Adapter (Solana-only; MWA has no concept of EVM chains at all). A user
 * who wants to transfer machine ownership connects a SECOND, EVM-capable
 * wallet (MetaMask, Rainbow, etc.) through this provider - the two wallet
 * connections are independent and a user can have either, both, or
 * neither connected at once.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  ReactNode,
} from 'react';
// Polyfills (TextEncoder/Decoder, crypto, URL) are set up once at the
// app's entry point (index.js), before anything else loads - not here.
import SignClient from '@walletconnect/sign-client';
import type {SessionTypes} from '@walletconnect/types';

import {PEAQ_CHAIN_ID} from '../../services/peaqTransfer';

// From https://cloud.reown.com - identifies this app to the WalletConnect
// relay network. Not a secret (every WalletConnect dApp ships its project
// ID in the client bundle) - it only controls relay/analytics quota, it
// grants no access to anything.
const WALLETCONNECT_PROJECT_ID = '43e49fdf5b12cc9df7e365549e67bb65';

type EvmWalletContextState = {
  address: string | null;
  connectUri: string | null;
  connecting: boolean;
  connect: () => Promise<void>;
  disconnect: () => Promise<void>;
  sendTransaction: (tx: {from: string; to: string; data: string}) => Promise<string>;
};

const EvmWalletContext = createContext<EvmWalletContextState | null>(null);

export function useEvmWallet(): EvmWalletContextState {
  const ctx = useContext(EvmWalletContext);
  if (!ctx) {
    throw new Error('useEvmWallet must be used inside an EvmWalletProvider');
  }
  return ctx;
}

export function EvmWalletProvider({children}: {children: ReactNode}) {
  const clientRef = useRef<InstanceType<typeof SignClient> | null>(null);
  const [address, setAddress] = useState<string | null>(null);
  const [session, setSession] = useState<SessionTypes.Struct | null>(null);
  const [connectUri, setConnectUri] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    SignClient.init({
      projectId: WALLETCONNECT_PROJECT_ID,
      metadata: {
        name: 'RoboPay CarOwnerApp',
        description: 'Manage your RoboPay machine - wallet, sensors, and ownership',
        url: 'https://staex.io/robopay',
        icons: [],
      },
    }).then(client => {
      if (cancelled) {
        return;
      }
      clientRef.current = client;

      // Resume an existing session if the app was reopened - avoids
      // forcing a fresh QR scan every launch.
      const existing = client.session.getAll();
      if (existing.length > 0) {
        const s = existing[existing.length - 1];
        setSession(s);
        setAddress(extractAddress(s));
      }

      client.on('session_delete', () => {
        setSession(null);
        setAddress(null);
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const connect = useCallback(async () => {
    const client = clientRef.current;
    if (!client) {
      throw new Error('WalletConnect client is not ready yet - try again in a moment');
    }
    setConnecting(true);
    setConnectUri(null);
    try {
      const {uri, approval} = await client.connect({
        requiredNamespaces: {
          eip155: {
            methods: ['eth_sendTransaction'],
            chains: [`eip155:${PEAQ_CHAIN_ID}`],
            events: ['chainChanged', 'accountsChanged'],
          },
        },
      });
      if (uri) {
        setConnectUri(uri);
      }
      const newSession = await approval();
      setSession(newSession);
      setAddress(extractAddress(newSession));
      setConnectUri(null);
    } finally {
      setConnecting(false);
    }
  }, []);

  const disconnect = useCallback(async () => {
    const client = clientRef.current;
    if (client && session) {
      await client.disconnect({
        topic: session.topic,
        reason: {code: 6000, message: 'User disconnected'},
      });
    }
    setSession(null);
    setAddress(null);
  }, [session]);

  const sendTransaction = useCallback(
    async (tx: {from: string; to: string; data: string}): Promise<string> => {
      const client = clientRef.current;
      if (!client || !session) {
        throw new Error('No connected EVM wallet - connect one first');
      }
      const result = await client.request<string>({
        topic: session.topic,
        chainId: `eip155:${PEAQ_CHAIN_ID}`,
        request: {
          method: 'eth_sendTransaction',
          params: [tx],
        },
      });
      return result;
    },
    [session],
  );

  return (
    <EvmWalletContext.Provider
      value={{address, connectUri, connecting, connect, disconnect, sendTransaction}}>
      {children}
    </EvmWalletContext.Provider>
  );
}

function extractAddress(session: SessionTypes.Struct): string | null {
  const accounts = session.namespaces.eip155?.accounts ?? [];
  if (accounts.length === 0) {
    return null;
  }
  // Account strings look like "eip155:3338:0xABC..." - we only use the
  // address portion.
  const parts = accounts[0].split(':');
  return parts[parts.length - 1] ?? null;
}
