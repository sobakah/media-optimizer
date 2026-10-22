// media-optimizer's page. Everything comes from /api/state (media_optimizer/web.py);
// the page never runs anything itself, it asks the server to.
import { html, render, useState, useEffect } from '/static/vendor/preact-htm.js';

async function api(path, body) {
  const options = {};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch('/api' + path, options);
  } catch (err) {
    throw new Error('media-optimizer is not responding. Is it still running?');
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function App() {
  const [state, setState] = useState(null);
  const [error, setError] = useState('');
  const [closed, setClosed] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      let rev = 0;
      while (alive) {
        try {
          const next = await api(`/state?since=${rev}`);
          rev = next.rev;
          setState(next);
          setError('');
          if (next.phase === 'finished') await new Promise((r) => setTimeout(r, 2000));
        } catch (err) {
          setError(err.message);
          await new Promise((r) => setTimeout(r, 2000));
        }
      }
    })();
    // The server counts the page as gone after the closing beacon (plus a
    // grace for a reload) or a long silence; pings keep it in.
    const ping = setInterval(() => api('/ping', {}).catch(() => {}), 15000);
    const closing = () => navigator.sendBeacon('/api/closing', '');
    addEventListener('pagehide', closing);
    return () => { alive = false; clearInterval(ping); removeEventListener('pagehide', closing); };
  }, []);

  const close = async () => {
    await api('/quit', {}).catch(() => {});
    if (state && state.returnUrl) location.href = state.returnUrl;
    else setClosed(true);
  };

  if (!state) return html`<main><p class="muted">Connecting…</p></main>`;
  const back = state.returnUrl ? `Back to ${state.returnName}` : 'Close';
  return html`
    <header class="topbar">
      <span class="brand"><img src="/static/icon.svg" alt="" width="26" height="26" /> media-optimizer
        <span class="version">${state.version}</span></span>
      <span class="spacer"></span>
      <button class="btn btn-small" onClick=${close}>${back}</button>
    </header>
    <main>
      <section class="section">
        <h2>Folder</h2>
        <p class="path mono">${state.source || 'No folder chosen yet.'}</p>
        <p class="muted">${state.target ? `Target: ${state.target}` : 'In place: originals are replaced.'}</p>
      </section>
    </main>
    ${error && html`<div class="toast" role="alert">${error}</div>`}
    ${closed && html`<div class="overlay"><div class="section"><h2>media-optimizer has ended</h2>
      <p class="muted">You can close this tab.</p></div></div>`}
  `;
}

render(html`<${App} />`, document.getElementById('app'));
