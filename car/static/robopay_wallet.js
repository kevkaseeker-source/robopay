/* RoboPay owner wallet for the seller dashboard (v2).
 *
 * The owner's keys live ONLY in this browser (localStorage) and every
 * transaction is signed here - the server never sees a private key. Same
 * features as the CarOwnerApp: Solana (devnet) and peaq (mainnet) identity
 * side by side, receive, send SOL / PEAQ / the Machine-NFT, and "take over
 * income" for a new owner (handoff steps 2+3 back to back, then sign in so
 * the server unlocks orders).
 *
 * Needs (loaded before this file): ethers 6 UMD, @solana/web3.js IIFE,
 * tweetnacl, qrcodejs. All server calls use RELATIVE urls so they work under
 * /seller/ and /v2/seller/ behind the gateway.
 */
(function () {
  'use strict';
  const CFG = {
    solRpc: 'https://api.devnet.solana.com',
    peaqRpc: 'https://peaq.api.onfinality.io/public',
    peaqChainId: 3338,
    registry: '0x64b93Cc29b251fAFa83BD110cDB1C24207f85536', // peaq MachineRegistry (Tokenomics 2.0, mainnet)
    solTx: 'https://explorer.solana.com/tx/{}?cluster=devnet',
    peaqTx: 'https://peaq.subscan.io/tx/{}',
  };
  // ?owner=A / ?owner=B: two separate owner wallets in the same browser, so one
  // laptop can play both the old and the new owner of the car's Machine-NFT.
  const PROFILE = (new URLSearchParams(location.search).get('owner') || '').toUpperCase();
  const ROLE = /^[AB]$/.test(PROFILE) ? 'CarOwner ' + PROFILE : '';
  const STORE = 'robopay.ownerWallet.v1' + (ROLE ? '.' + PROFILE : '');
  const REG_ABI = [
    'function ownerOf(uint256) view returns (address)',
    'function controllerOf(uint256) view returns (address)',
    'function transferFrom(address,address,uint256)',
    'function setController(uint256,address)',
    'function setVerificationMethods(uint256,(string,string,address,string)[])',
    'function didDocument(uint256) view returns ((string,address,(string,string,address,string)[],uint256[],(string,string,string)[]))',
  ];
  const METHOD_ID = '#solana-owner', METHOD_TYPE = 'Ed25519VerificationKey2020';

  // ---------------------------------------------------------------- base58 / multibase
  const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  function b58encode(bytes) {
    let n = 0n;
    for (const b of bytes) n = n * 256n + BigInt(b);
    let out = '';
    while (n > 0n) { out = B58[Number(n % 58n)] + out; n /= 58n; }
    for (const b of bytes) { if (b !== 0) break; out = '1' + out; }
    return out;
  }
  function b58decode(str) {
    let n = 0n;
    for (const c of str) {
      const i = B58.indexOf(c);
      if (i < 0) throw new Error('not base58');
      n = n * 58n + BigInt(i);
    }
    const bytes = [];
    while (n > 0n) { bytes.unshift(Number(n % 256n)); n /= 256n; }
    for (const c of str) { if (c !== '1') break; bytes.unshift(0); }
    return Uint8Array.from(bytes);
  }
  // did:key-style Ed25519: 'z' + base58(0xed 0x01 + 32-byte key) - same as rpi/set_payout_wallet.py
  const multibase = (pub) => 'z' + b58encode(Uint8Array.from([0xed, 0x01, ...pub]));
  function fromMultibase(mb) {
    if (!mb || mb[0] !== 'z') return null;
    const raw = b58decode(mb.slice(1));
    return raw.length === 34 && raw[0] === 0xed && raw[1] === 0x01 ? b58encode(raw.slice(2)) : null;
  }

  // ---------------------------------------------------------------- keys (browser only)
  function loadKeys() {
    try { return JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (e) { return null; }
  }
  function saveKeys(k) { localStorage.setItem(STORE, JSON.stringify(k)); }
  function parseSolanaSecret(text) {
    text = text.trim();
    const bytes = text.startsWith('[') ? Uint8Array.from(JSON.parse(text)) : b58decode(text);
    if (bytes.length !== 64) throw new Error('Solana-Schlüssel muss 64 Bytes haben (JSON-Liste oder Base58)');
    return solanaWeb3.Keypair.fromSecretKey(bytes); // throws if the pair doesn't match
  }
  function parsePeaqKey(text) {
    text = text.trim();
    if (!text.startsWith('0x')) text = '0x' + text;
    return new ethers.Wallet(text); // throws on a bad key
  }
  const solKp = (k) => solanaWeb3.Keypair.fromSecretKey(Uint8Array.from(k.solana));
  const peaqProvider = () => new ethers.JsonRpcProvider(CFG.peaqRpc, CFG.peaqChainId, { staticNetwork: true });
  const peaqWallet = (k) => new ethers.Wallet(k.peaq, peaqProvider());
  const solConn = () => new solanaWeb3.Connection(CFG.solRpc, 'confirmed');
  const registry = (signerOrProvider) => new ethers.Contract(CFG.registry, REG_ABI, signerOrProvider);

  // ---------------------------------------------------------------- chain reads
  async function machineStatus() {
    const r = await fetch('machine_status');
    return r.json();
  }
  async function nftState(machineId) {
    const reg = registry(peaqProvider());
    const [owner, controller, doc] = await Promise.all([
      reg.ownerOf(machineId), reg.controllerOf(machineId), reg.didDocument(machineId)]);
    const payees = doc[2].filter((vm) => vm[1] === METHOD_TYPE).map((vm) => ({
      id: vm[0], controller: vm[2], solana: fromMultibase(vm[3]) }));
    return { owner, controller, payees, otherEntries: doc[2].length - payees.length };
  }

  // ---------------------------------------------------------------- writes
  // Tell the server about a confirmed transfer so every page lists it. The
  // server looks it up on chain itself; failures here never break a send.
  function report(chain, kind, tx) {
    fetch('activity', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chain, kind, tx }) })
      .then(() => window.RoboPayUI && window.RoboPayUI.refresh()).catch(() => {});
  }
  async function sendSol(k, to, amountSol) {
    if (typeof Buffer === 'undefined') throw new Error('Seite ist noch nicht fertig geladen - bitte kurz warten oder neu laden');
    const dest = new solanaWeb3.PublicKey(to);
    const lamports = Math.round(Number(amountSol) * solanaWeb3.LAMPORTS_PER_SOL);
    if (!(lamports > 0)) throw new Error('Betrag ungültig');
    const kp = solKp(k);
    if (dest.equals(kp.publicKey)) throw new Error('Das ist deine eigene Adresse');
    const tx = new solanaWeb3.Transaction().add(
      solanaWeb3.SystemProgram.transfer({ fromPubkey: kp.publicKey, toPubkey: dest, lamports }));
    const sig = await solanaWeb3.sendAndConfirmTransaction(solConn(), tx, [kp]);
    report('solana', 'sol', sig);
    return CFG.solTx.replace('{}', sig);
  }
  async function sendPeaq(k, to, amountPeaq) {
    if (!ethers.isAddress(to)) throw new Error('Keine gültige peaq-Adresse');
    const w = peaqWallet(k);
    if (ethers.getAddress(to) === w.address) throw new Error('Das ist deine eigene Adresse');
    const tx = await w.sendTransaction({ to: ethers.getAddress(to), value: ethers.parseEther(String(amountPeaq)) });
    await tx.wait();
    report('peaq', 'peaq', tx.hash);
    return CFG.peaqTx.replace('{}', tx.hash);
  }
  async function transferNft(k, machineId, to) {
    if (!ethers.isAddress(to)) throw new Error('Keine gültige peaq-Adresse');
    to = ethers.getAddress(to);
    const st = await machineStatus();
    // Kevin's rule: the car's owner must not change during a delivery.
    if (st.busy) throw new Error('Gerade läuft eine Lieferung - das NFT kann erst danach übertragen werden');
    const w = peaqWallet(k), reg = registry(w);
    if (to === w.address) throw new Error('Das ist deine eigene Adresse');
    if ((await reg.ownerOf(machineId)) !== w.address) throw new Error('Diese Wallet besitzt das NFT nicht');
    if ((await w.provider.getCode(to)) !== '0x') throw new Error('Empfänger ist ein Vertrag, keine Wallet - abgebrochen');
    await reg.transferFrom.staticCall(w.address, to, machineId); // simulate first
    const tx = await reg.transferFrom(w.address, to, machineId);
    await tx.wait();
    report('peaq', 'nft', tx.hash);
    return CFG.peaqTx.replace('{}', tx.hash);
  }
  // Handoff steps 2 and 3 back to back (closes the window in which an entry
  // the old owner planted could be trusted), then sign in so the server
  // acknowledges the new owner and unlocks orders.
  async function takeOver(k, machineId, log) {
    const w = peaqWallet(k), reg = registry(w), kp = solKp(k);
    if ((await reg.ownerOf(machineId)) !== w.address) throw new Error('Diese Wallet besitzt das NFT nicht');
    const st = await nftState(machineId);
    if (st.otherEntries > 0) throw new Error('Das DID enthält weitere Einträge - bitte manuell prüfen');
    if (st.controller !== w.address) {
      await reg.setController.staticCall(machineId, w.address);
      const t2 = await reg.setController(machineId, w.address);
      log('Schritt 2/3: DID-Kontrolle übernommen ' + t2.hash);
      await t2.wait();
    }
    const entry = [METHOD_ID, METHOD_TYPE, w.address, multibase(kp.publicKey.toBytes())];
    await reg.setVerificationMethods.staticCall(machineId, [entry]);
    const t3 = await reg.setVerificationMethods(machineId, [entry]);
    log('Schritt 3/3: Solana-Wallet eingetragen ' + t3.hash);
    await t3.wait();
    const after = await nftState(machineId);
    if (after.payees.length !== 1 || after.payees[0].solana !== kp.publicKey.toBase58())
      throw new Error('Prüfung fehlgeschlagen: das DID zahlt nicht an deine Wallet');
    await signIn(k);
    return CFG.peaqTx.replace('{}', t3.hash);
  }
  async function signIn(k) {
    const kp = solKp(k);
    const { message } = await (await fetch('mobile/challenge')).json();
    const sig = nacl.sign.detached(new TextEncoder().encode(message), kp.secretKey);
    const r = await fetch('mobile/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pubkey: kp.publicKey.toBase58(), message, signature: b58encode(sig) }) });
    if (!r.ok) throw new Error('Anmeldung abgelehnt: ' + ((await r.json()).error || r.status));
  }

  // ---------------------------------------------------------------- UI
  const $ = (id) => document.getElementById(id);
  const short = (a) => a ? a.slice(0, 6) + '…' + a.slice(-4) : '—';
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function say(msg, isError) {
    const el = $('rpwMsg');
    el.style.color = isError ? '#f87171' : '#4ade80';
    el.innerHTML = msg;
  }
  async function act(label, fn) {
    try { say(label + ' …'); const link = await fn(); say(label + ' ✓' + (link ? ` <a href="${esc(link)}" target="_blank">Transaktion ansehen</a>` : '')); refresh(); }
    catch (e) { say(label + ' fehlgeschlagen: ' + esc(e.shortMessage || e.message || e), true); }
  }
  const copyBtn = (t) => (window.RoboPayUI ? window.RoboPayUI.copyButton(t) : '');
  // CarOwner A/B: let the server know this owner's PUBLIC addresses, so every
  // page (also on other devices) can name them. Never sends a key.
  function registerOwner(sol, evm) {
    if (!ROLE) return;
    fetch('activity/owner', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role: PROFILE, sol, evm }) }).catch(() => {});
  }

  function renderSetup() {
    $('rpWallet').innerHTML = `
      <div class="label">${ROLE ? esc(ROLE) + ' · ' : ''}Meine Wallet (nur in diesem Browser)</div>
      <p style="color:#aaa;font-size:0.85rem;">Deine Schlüssel werden nur in diesem Browser gespeichert und nie an den Server geschickt. Demo-Wallet - nicht für große Beträge.</p>
      <label class="label">Solana-Schlüssel (JSON-Liste oder Base58)</label>
      <textarea id="rpwSol" rows="2" style="width:100%" autocomplete="off" spellcheck="false"></textarea>
      <label class="label">peaq-Schlüssel (0x…)</label>
      <input id="rpwPeaq" type="password" style="width:100%" autocomplete="off">
      <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;">
        <button class="btn-small" id="rpwImport">Schlüssel importieren</button>
        <button class="btn-small" id="rpwNew">Neue Wallets erzeugen</button>
      </div>
      <div id="rpwMsg" style="margin-top:8px;font-size:0.85rem;"></div>`;
    $('rpwImport').onclick = () => {
      try {
        const sol = parseSolanaSecret($('rpwSol').value), peaq = parsePeaqKey($('rpwPeaq').value);
        saveKeys({ solana: Array.from(sol.secretKey), peaq: peaq.privateKey });
        $('rpwSol').value = ''; $('rpwPeaq').value = '';
        render();
      } catch (e) { say('Import fehlgeschlagen: ' + esc(e.message), true); }
    };
    $('rpwNew').onclick = () => {
      if (!confirm('Neue Solana- und peaq-Wallet in diesem Browser erzeugen? Sichere danach die Schlüssel über "Schlüssel anzeigen".')) return;
      saveKeys({ solana: Array.from(solanaWeb3.Keypair.generate().secretKey), peaq: ethers.Wallet.createRandom().privateKey });
      render();
    };
  }

  function renderWallet(k) {
    const kp = solKp(k), w = new ethers.Wallet(k.peaq);
    $('rpWallet').innerHTML = `
      <div class="label">${ROLE ? esc(ROLE) + ' · ' : ''}Meine Wallet (nur in diesem Browser)</div>
      <div class="rpw-grid">
        <div class="rpw-card">
          <div class="label">Solana · Devnet</div>
          <div class="wallet-amount" id="rpwSolBal">…</div>
          <div class="wallet-addr">${kp.publicKey.toBase58()}${copyBtn(kp.publicKey.toBase58())}</div>
          <input id="rpwSolTo" placeholder="Empfänger (Solana-Adresse)">
          <input id="rpwSolAmt" placeholder="Betrag SOL" inputmode="decimal">
          <button class="btn-small" id="rpwSolSend">SOL senden</button>
        </div>
        <div class="rpw-card">
          <div class="label">peaq · Mainnet</div>
          <div class="wallet-amount" id="rpwPeaqBal">…</div>
          <div class="wallet-addr">${w.address}${copyBtn(w.address)}</div>
          <input id="rpwPeaqTo" placeholder="Empfänger (0x…)">
          <input id="rpwPeaqAmt" placeholder="Betrag PEAQ" inputmode="decimal">
          <button class="btn-small" id="rpwPeaqSend">PEAQ senden</button>
        </div>
        <div class="rpw-card">
          <div class="label">Machine-NFT (Auto) · peaq Mainnet</div>
          <div id="rpwNft" class="wallet-addr">…</div>
          <input id="rpwNftTo" placeholder="Neuer Besitzer (0x…)">
          <button class="btn-small" id="rpwNftSend">NFT senden</button>
          <button class="btn-small" id="rpwTakeOver" style="margin-top:6px;">Einnahmen übernehmen</button>
        </div>
      </div>
      <div id="rpwMsg" style="margin-top:8px;font-size:0.85rem;"></div>
      <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn-small" id="rpwShow">Schlüssel anzeigen</button>
        <button class="btn-small" id="rpwForget">Wallet aus diesem Browser entfernen</button>
      </div>`;
    registerOwner(kp.publicKey.toBase58(), w.address);
    $('rpwSolSend').onclick = () => {
      const to = $('rpwSolTo').value.trim(), amt = $('rpwSolAmt').value.trim();
      if (confirm(`${amt} SOL (Devnet) an ${to} senden?`)) act('SOL senden', () => sendSol(k, to, amt));
    };
    $('rpwPeaqSend').onclick = () => {
      const to = $('rpwPeaqTo').value.trim(), amt = $('rpwPeaqAmt').value.trim();
      if (confirm(`${amt} PEAQ (Mainnet, echtes Geld) an ${to} senden?`)) act('PEAQ senden', () => sendPeaq(k, to, amt));
    };
    $('rpwNftSend').onclick = async () => {
      const to = $('rpwNftTo').value.trim();
      const check = prompt(`Das Auto-NFT an ${to} übertragen? Das ist auf Mainnet und nicht rückgängig zu machen.\nZur Bestätigung die letzten 4 Zeichen der Empfänger-Adresse eingeben:`);
      if (check === null) return;
      if (check.toLowerCase() !== to.slice(-4).toLowerCase()) return say('Bestätigung stimmt nicht - nichts gesendet', true);
      const st = await machineStatus();
      act('NFT senden', () => transferNft(k, BigInt(st.machine_id), to));
    };
    $('rpwShow').onclick = () => {
      if (!confirm('Schlüssel im Klartext anzeigen? Achte darauf, dass niemand zusieht und nichts aufgezeichnet wird.')) return;
      say(`Solana: <span class="mono">${esc(JSON.stringify(k.solana))}</span><br>peaq: <span class="mono">${esc(k.peaq)}</span>`);
    };
    $('rpwForget').onclick = () => {
      if (confirm('Wallet aus diesem Browser entfernen? Ohne Sicherung sind die Schlüssel weg.')) { localStorage.removeItem(STORE); render(); }
    };
    refresh();
  }

  async function refresh() {
    const k = loadKeys();
    if (!k) return;
    const kp = solKp(k), w = new ethers.Wallet(k.peaq);
    solConn().getBalance(kp.publicKey).then((l) => { $('rpwSolBal').textContent = (l / solanaWeb3.LAMPORTS_PER_SOL).toFixed(4) + ' SOL'; })
      .catch(() => { $('rpwSolBal').textContent = '— SOL'; });
    peaqProvider().getBalance(w.address).then((b) => { $('rpwPeaqBal').textContent = Number(ethers.formatEther(b)).toFixed(4) + ' PEAQ'; })
      .catch(() => { $('rpwPeaqBal').textContent = '— PEAQ'; });
    try {
      const st = await machineStatus(), nft = await nftState(BigInt(st.machine_id));
      const mine = nft.owner === w.address;
      const paysMe = nft.payees.length === 1 && nft.payees[0].solana === kp.publicKey.toBase58() && nft.controller === w.address;
      const named = (a) => { const n = whoIs(a); return n ? ` <b>= ${esc(n)}</b>` : ''; };
      $('rpwNft').innerHTML = `Machine-ID ${short(st.machine_id)}<br>Besitzer: ${short(nft.owner)}${named(nft.owner)}<br>`
        + `Auszahlung an: ${nft.payees.map((p) => short(p.solana) + named(p.solana)).join(', ') || '—'}<br>`
        + (st.busy ? '<span style="color:#facc15">Lieferung läuft - Übertragung gesperrt</span>' : '')
        + (st.handoff_pending ? '<span style="color:#facc15">Neuer Besitzer muss Einnahmen übernehmen</span>' : '');
      $('rpwNftSend').disabled = !mine || st.busy;
      $('rpwTakeOver').disabled = !mine || (paysMe && !st.handoff_pending);
      const onlySignIn = mine && paysMe && st.handoff_pending;
      $('rpwTakeOver').textContent = onlySignIn ? 'Anmelden (Bestellungen freigeben)' : 'Einnahmen übernehmen';
      $('rpwTakeOver').onclick = onlySignIn
        ? () => act('Anmelden', () => signIn(k).then(() => null))
        : () => { if (confirm('Einnahmen übernehmen: DID-Kontrolle übernehmen und deine Solana-Wallet eintragen (2 Transaktionen auf peaq Mainnet)?'))
            act('Einnahmen übernehmen', () => takeOver(k, BigInt(st.machine_id), (m) => say(esc(m)))); };
    } catch (e) {
      $('rpwNft').textContent = 'NFT-Status nicht lesbar: ' + (e.shortMessage || e.message);
    }
  }

  function render() {
    const k = loadKeys();
    if (k) renderWallet(k); else renderSetup();
    renderRole();
  }

  // Public addresses of CarOwner A and B, derived from the wallets stored in
  // this browser, so the car's panels can say whose address they show.
  const _idCache = {};
  function identity(p) {
    let raw = null;
    try { raw = localStorage.getItem('robopay.ownerWallet.v1.' + p); } catch (e) { return null; }
    if (!raw) return null;
    if (_idCache[p] && _idCache[p].raw === raw) return _idCache[p];
    try {
      const k = JSON.parse(raw);
      _idCache[p] = { raw, sol: solKp(k).publicKey.toBase58(), evm: new ethers.Wallet(k.peaq).address };
      return _idCache[p];
    } catch (e) { return null; }
  }
  function whoIs(addr) {
    if (!addr) return '';
    for (const p of ['A', 'B']) {
      const id = identity(p);
      if (id && (id.sol === addr || id.evm.toLowerCase() === String(addr).toLowerCase()))
        return 'CarOwner ' + p + (p === PROFILE ? ' (du)' : '');
    }
    return '';
  }
  function renderRole() {
    const el = $('rpRole');
    if (!el || !ROLE) return;
    const id = identity(PROFILE);
    el.innerHTML = `<div class="role-bar role-${PROFILE}"><b>Du bist ${esc(ROLE)}</b>`
      + (id ? `<span>Solana <span class="mono">${esc(id.sol)}</span>${copyBtn(id.sol)}</span><span>peaq <span class="mono">${esc(id.evm)}</span>${copyBtn(id.evm)}</span>`
            : '<span>Noch keine Wallet in diesem Browser. Unten Schlüssel importieren oder neue Wallets erzeugen.</span>')
      + '</div>';
  }

  window.RoboPayWallet = { render, refresh, whoIs, _test: { b58encode, b58decode, multibase, fromMultibase } };
  document.addEventListener('DOMContentLoaded', () => { if ($('rpWallet')) { render(); setInterval(refresh, 30000); } });
})();
