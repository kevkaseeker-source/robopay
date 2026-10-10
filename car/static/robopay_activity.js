/* RoboPay shared transaction view - the same on the buyer page and on both
 * car-owner pages (CarOwner A / CarOwner B).
 *
 * Shows every delivery (Solana: "Auto bestellt" -> "Escrow released") and
 * every wallet transfer (Solana: SOL; peaq: Machine-NFT and PEAQ), each with a
 * link to its block explorer. Addresses are named and coloured by role:
 * Buyer green, CarOwner A blue, CarOwner B orange, the car grey. A toast
 * announces new orders, deliveries and NFT transfers.
 *
 * Also provides RoboPayUI.copyButton() for "copy address" buttons.
 * Needs a <div id="rpActivity"> on the page; all requests are relative so it
 * works under /buyer/ and /seller/ behind the gateway.
 */
(function () {
  'use strict';
  const SOL_TX = (s) => `https://explorer.solana.com/tx/${s}?cluster=devnet`;
  const SOL_ADDR = (a) => `https://explorer.solana.com/address/${a}?cluster=devnet`;
  const PEAQ_TX = (h) => `https://peaq.subscan.io/tx/${h}`;
  const PEAQ_ADDR = (a) => `https://peaq.subscan.io/account/${a}`;
  const ROLE_STYLE = {
    buyer: { label: 'Buyer', color: '#4ade80' },
    A: { label: 'CarOwner A', color: '#60a5fa' },
    B: { label: 'CarOwner B', color: '#f59e0b' },
    car: { label: 'Auto', color: '#9ca3af' },
  };
  // Which role is looking at this page: the buyer page sets data-role="buyer"
  // on <body>; the owner pages are /seller/?owner=A|B.
  const owner = (new URLSearchParams(location.search).get('owner') || '').toUpperCase();
  const ME = document.body && document.body.dataset.role === 'buyer' ? 'buyer' : (/^[AB]$/.test(owner) ? owner : '');
  const IS_SELLER = !(document.body && document.body.dataset.role === 'buyer');

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const short = (a) => (a && a.length > 14 ? a.slice(0, 6) + '…' + a.slice(-4) : (a || '—'));
  const when = (t) => t ? new Date(t * 1000).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';

  // ------------------------------------------------------------- copy button
  function copyButton(text) {
    return `<button type="button" class="rp-copy" data-copy="${esc(text)}" title="Adresse kopieren">⧉</button>`;
  }
  document.addEventListener('click', async (e) => {
    const b = e.target.closest && e.target.closest('.rp-copy');
    if (!b) return;
    e.preventDefault();
    const text = b.dataset.copy;
    try { await navigator.clipboard.writeText(text); }
    catch (err) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (e2) {}
      ta.remove();
    }
    const old = b.textContent; b.textContent = '✓'; b.classList.add('rp-copied');
    setTimeout(() => { b.textContent = old; b.classList.remove('rp-copied'); }, 1200);
  });

  // ------------------------------------------------------------- styles
  const css = `
    .rp-copy { background:#2a2a2a; color:#ddd; border:1px solid #444; border-radius:4px; padding:1px 6px; margin-left:6px;
      font-size:0.8rem; line-height:1.3; cursor:pointer; width:auto; font-weight:400; vertical-align:middle; }
    .rp-copy:hover { background:#3a3a3a; } .rp-copy.rp-copied { color:#4ade80; border-color:#4ade80; }
    #rpActivity h3 { font-size:0.8rem; letter-spacing:0.06em; text-transform:uppercase; color:#9ca3af; margin:18px 0 6px; font-weight:600; }
    #rpActivity h3:first-of-type { margin-top:6px; }
    #rpActivity .rp-chain { font-size:0.95rem; color:#eee; margin:16px 0 2px; font-weight:700; }
    #rpActivity table { width:100%; border-collapse:collapse; font-size:0.82rem; }
    #rpActivity th { text-align:left; color:#888; font-weight:500; padding:4px 8px 4px 0; border-bottom:1px solid #333; white-space:nowrap; }
    #rpActivity td { padding:6px 8px 6px 0; border-bottom:1px solid #262626; vertical-align:top; }
    #rpActivity .rp-scroll { overflow-x:auto; }
    .rp-who { display:inline-block; padding:1px 7px; border-radius:10px; color:#111; font-weight:600; font-size:0.75rem; white-space:nowrap; }
    .rp-who.rp-me { outline:2px solid #fff; }
    .rp-addr { font-family:ui-monospace,monospace; font-size:0.75rem; color:#aaa; }
    .rp-st-pending { color:#facc15; } .rp-st-delivered { color:#4ade80; } .rp-st-refunded { color:#60a5fa; } .rp-st-closed { color:#888; }
    .rp-legend { display:flex; gap:8px; flex-wrap:wrap; margin:4px 0 6px; }
    .rp-empty { color:#777; font-size:0.85rem; padding:4px 0; }
    #rpToasts { position:fixed; right:16px; bottom:16px; display:flex; flex-direction:column; gap:8px; z-index:1000; max-width:min(380px, calc(100vw - 32px)); }
    .rp-toast { background:#1f2937; color:#eee; border-left:4px solid #4ade80; border-radius:8px; padding:12px 14px; box-shadow:0 4px 16px rgba(0,0,0,.5); font-size:0.9rem; }
    .rp-toast b { display:block; margin-bottom:2px; }`;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  // ------------------------------------------------------------- names
  let FEED = null;
  function roleOf(addr) {
    if (!addr || !FEED) return null;
    const a = String(addr).toLowerCase();
    if (FEED.buyer && FEED.buyer.toLowerCase() === a) return 'buyer';
    for (const r of ['A', 'B']) {
      const o = (FEED.owners || {})[r];
      if (o && (o.sol.toLowerCase() === a || o.evm.toLowerCase() === a)) return r;
    }
    if (FEED.operator && FEED.operator.toLowerCase() === a) return 'car';
    return null;
  }
  function who(addr, chain) {
    const r = roleOf(addr);
    const link = chain === 'peaq' ? PEAQ_ADDR(addr) : SOL_ADDR(addr);
    const addrHtml = `<a class="rp-addr" href="${esc(link)}" target="_blank" rel="noopener">${esc(short(addr))}</a>`;
    if (!r) return addr ? addrHtml : '—';
    const s = ROLE_STYLE[r];
    const me = r === ME;
    return `<span class="rp-who${me ? ' rp-me' : ''}" style="background:${s.color}">${esc(s.label)}${me ? ' (du)' : ''}</span> ${addrHtml}`;
  }
  const txLink = (url, id, label) => id ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(label || short(id))}</a>` : '—';
  const STATUS = { pending: 'pending (unterwegs)', delivered: 'delivered', refunded: 'zurückgebucht', closed: 'abgeschlossen' };

  // ------------------------------------------------------------- render
  function render(f) {
    const el = document.getElementById('rpActivity');
    if (!el) return;
    const legend = ['buyer', 'A', 'B', 'car'].map((r) =>
      `<span class="rp-who${r === ME ? ' rp-me' : ''}" style="background:${ROLE_STYLE[r].color}">${ROLE_STYLE[r].label}${r === ME ? ' (du)' : ''}</span>`).join('');

    const deliveries = f.deliveries.map((d) => `<tr>
        <td><b>#${d.n}</b></td>
        <td>${when(d.ordered_at)}</td>
        <td>${who(d.buyer, 'solana')}</td>
        <td>${d.payee ? who(d.payee, 'solana') : '<span class="rp-empty">—</span>'}</td>
        <td class="rp-st-${esc(d.status)}">${esc(STATUS[d.status] || d.status)}</td>
        <td>${txLink(SOL_TX(d.order_tx), d.order_tx)}</td>
        <td>${d.release_tx ? txLink(SOL_TX(d.release_tx), d.release_tx) + (IS_SELLER ? ` · <a href="gallery/${encodeURIComponent(d.order_tx)}" target="_blank">Sensordaten</a>` : '')
              : d.refund_tx ? 'Rückbuchung ' + txLink(SOL_TX(d.refund_tx), d.refund_tx) : '—'}</td>
      </tr>`).join('');

    const transfers = (list, chain, unit, kindLabel) => list.length ? `<div class="rp-scroll"><table>
        <tr><th>Zeit</th><th>Von</th><th>An</th><th>${kindLabel}</th><th>Transaktion</th></tr>
        ${list.map((e) => `<tr><td>${when(e.t)}</td><td>${who(e.from, chain)}</td><td>${who(e.to, chain)}</td>
          <td>${unit === 'NFT' ? 'Machine-NFT ' + esc(short(e.token_id)) : esc(Number(e.amount).toLocaleString('de-DE', { maximumFractionDigits: 4 })) + ' ' + unit}</td>
          <td>${txLink(chain === 'peaq' ? PEAQ_TX(e.tx) : SOL_TX(e.tx), e.tx)}</td></tr>`).join('')}
      </table></div>` : '<div class="rp-empty">— noch keine —</div>';

    el.innerHTML = `
      <div class="label">Transaktionen · gesamtes System</div>
      <div class="rp-legend">${legend}</div>

      <div class="rp-chain">Solana · Devnet</div>
      <h3>Lieferungen: „Auto bestellt“ → „Escrow released“</h3>
      ${f.deliveries.length ? `<div class="rp-scroll"><table>
        <tr><th>Lieferung</th><th>Bestellt</th><th>Bestellt von</th><th>Zahlt an</th><th>Status</th><th>Auto bestellt</th><th>Escrow released</th></tr>
        ${deliveries}</table></div>` : '<div class="rp-empty">— noch keine Lieferungen —</div>'}
      <h3>SOL versendet</h3>
      ${transfers(f.sol_transfers, 'solana', 'SOL', 'Betrag')}

      <div class="rp-chain">peaq · Mainnet</div>
      <h3>NFT versendet</h3>
      ${transfers(f.nft_transfers, 'peaq', 'NFT', 'Was')}
      <h3>PEAQ versendet</h3>
      ${transfers(f.peaq_transfers, 'peaq', 'PEAQ', 'Betrag')}`;
  }

  // ------------------------------------------------------------- toasts
  function toast(title, text) {
    let box = document.getElementById('rpToasts');
    if (!box) { box = document.createElement('div'); box.id = 'rpToasts'; document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = 'rp-toast';
    t.innerHTML = `<b>${esc(title)}</b>${text}`;
    box.appendChild(t);
    setTimeout(() => t.remove(), 9000);
  }
  const plainName = (addr) => { const r = roleOf(addr); return r ? ROLE_STYLE[r].label : short(addr); };
  let prev = null;
  function announce(f) {
    if (prev) {
      const old = new Map(prev.deliveries.map((d) => [d.n, d.status]));
      for (const d of f.deliveries) {
        if (!old.has(d.n)) toast(`Neue Bestellung #${d.n}`, `${esc(plainName(d.buyer))} hat das Auto bestellt · Status: pending`);
        else if (old.get(d.n) === 'pending' && d.status === 'delivered')
          toast(`Lieferung #${d.n}: pending → delivered`, `Escrow released an ${esc(plainName(d.payee))}`);
        else if (old.get(d.n) === 'pending' && d.status === 'refunded')
          toast(`Lieferung #${d.n} zurückgebucht`, 'Das Geld ist zurück beim Buyer');
      }
      const seen = new Set(prev.nft_transfers.map((e) => e.tx));
      for (const e of f.nft_transfers) if (!seen.has(e.tx))
        toast('Machine-NFT versendet', `${esc(plainName(e.from))} → ${esc(plainName(e.to))}`);
    }
    prev = f;
  }

  async function poll() {
    try {
      const r = await fetch('activity', { cache: 'no-store' });
      if (!r.ok) return;
      const f = await r.json();
      FEED = f;
      announce(f);
      render(f);
    } catch (e) {}
  }

  window.RoboPayUI = { copyButton, refresh: poll, roleOf: (a) => roleOf(a) };
  const start = () => { poll(); setInterval(poll, 4000); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
