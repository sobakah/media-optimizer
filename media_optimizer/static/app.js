// media-optimizer's page. Everything comes from the server (media_optimizer/web.py):
// /api/setup for the form, /api/state for the run. The run itself is the real
// program (run -y); the page only starts it, shows it and can stop it.
import { html, render, useState, useEffect, useRef } from '/static/vendor/preact-htm.js';

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
  if (!response.ok) {
    const error = new Error(data.error || `${response.status} ${response.statusText}`);
    error.fields = data.fields || null;
    throw error;
  }
  return data;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const baseName = (path) => String(path || '').split('/').pop();

function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function saving(before, after) {
  if (!before || !after) return '';
  const pct = Math.round((after - before) * 100 / before);
  return `${formatBytes(before)} → ${formatBytes(after)} (${pct > 0 ? '+' : ''}${pct}%)`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return '';
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
}

// The terminal's colour codes (ui._ANSI) as spans; text stays text, other
// control sequences are dropped.
const SGR = { 1: 'b', 2: 'dim', 31: 'red', 32: 'green', 33: 'yellow', 34: 'blue', 35: 'magenta', 36: 'cyan', 42: 'bg-green' };
const ESC = /\x1b\[([0-9;?]*)([A-Za-z])/g;

function AnsiLine({ text }) {
  const parts = [];
  let classes = [];
  let last = 0;
  const push = (chunk) => {
    if (!chunk) return;
    parts.push(classes.length ? html`<span class=${classes.map((c) => 'ansi-' + c).join(' ')}>${chunk}</span>` : chunk);
  };
  for (const match of text.matchAll(ESC)) {
    push(text.slice(last, match.index));
    last = match.index + match[0].length;
    if (match[2] !== 'm') continue;
    for (const code of (match[1] || '0').split(';')) {
      if (code === '0' || code === '') classes = [];
      else if (SGR[code]) classes = [...classes.filter((c) => c !== SGR[code]), SGR[code]];
    }
  }
  push(text.slice(last));
  return parts;
}

// --- the form ------------------------------------------------------------------------
const ORIGINS = { config: 'from the config file', env: 'from the environment', cli: 'from the command line' };

function shown(when, values, hasTarget) {
  if (!when || !when.length) return true;
  const [name, ...allowed] = when;
  const value = name === 'target' ? hasTarget : values[name];
  return allowed.includes(value);
}

function Field({ field, value, onChange, label, error }) {
  const id = 'f-' + field.name;
  const title = label || field.name;
  const origin = field.origin !== 'default' ? html`<span class="origin">${ORIGINS[field.origin] || field.origin}</span>` : '';
  let input;
  if (field.kind === 'bool') {
    return html`<div class=${'field check' + (error ? ' invalid' : '')}>
      <label><input type="checkbox" id=${id} checked=${!!value} onChange=${(e) => onChange(e.target.checked)} />
        <span>${title}</span></label>${origin}
      ${!label && html`<p class="doc">${field.doc}</p>`}${error && html`<p class="error">${error}</p>`}
    </div>`;
  }
  if (field.kind === 'choice') {
    input = html`<select id=${id} value=${value} onChange=${(e) => onChange(e.target.value)}>
      ${field.choices.map((c) => html`<option value=${c}>${c}</option>`)}</select>`;
  } else if (field.kind === 'int' || field.kind === 'num') {
    input = html`<input type="number" id=${id} min=${field.min} max=${field.max} step=${field.kind === 'int' ? 1 : 'any'}
      placeholder=${field.nullable ? 'automatic' : ''} value=${value === null ? '' : value}
      onChange=${(e) => {
        const text = e.target.value.trim();
        if (text === '') onChange(field.nullable ? null : text);
        else onChange(field.kind === 'int' ? (/^-?\d+$/.test(text) ? parseInt(text, 10) : text) : Number(text));
      }} />`;
  } else if (field.kind === 'words') {
    input = html`<input type="text" id=${id} value=${(value || []).join(' ')}
      onChange=${(e) => onChange(e.target.value.split(/\s+/).filter(Boolean))} />`;
  } else if (field.kind === 'paths') {
    input = html`<textarea id=${id} rows="3" value=${(value || []).join('\n')}
      onChange=${(e) => onChange(e.target.value.split('\n').map((s) => s.trim()).filter(Boolean))}></textarea>`;
  } else if (field.kind === 'file_or_off') {
    input = html`<span class="row">
      <label class="inline"><input type="checkbox" checked=${value === false}
        onChange=${(e) => onChange(e.target.checked ? false : null)} /> off</label>
      <input type="text" id=${id} disabled=${value === false} placeholder="default file"
        value=${typeof value === 'string' ? value : ''} onInput=${(e) => onChange(e.target.value.trim() || null)} /></span>`;
  } else {
    input = html`<input type="text" id=${id} value=${value === null ? '' : value}
      onInput=${(e) => onChange(field.nullable && !e.target.value ? null : e.target.value)} />`;
  }
  return html`<div class=${'field' + (error ? ' invalid' : '')}>
    <label for=${id}>${title}</label>${origin}
    ${input}
    ${!label && html`<p class="doc">${field.doc}</p>`}${error && html`<p class="error">${error}</p>`}
  </div>`;
}

function Question({ q, field, values, setValue, hasTarget, error }) {
  if (q.special === 'delete') {
    // As the terminal: the default follows the mode, the answer counts as chosen.
    const value = values.delete_original === null ? !hasTarget : values.delete_original;
    return html`<${Field} field=${{ ...field, kind: 'bool' }} label=${q.text} value=${value}
      onChange=${(v) => setValue('delete_original', v)} error=${error} />`;
  }
  if (q.special === 'rescan') {
    return html`<${Field} field=${{ ...field, kind: 'bool' }} label=${q.text} value=${!values.use_cache}
      onChange=${(v) => setValue('use_cache', !v)} error=${error} />`;
  }
  return html`<${Field} field=${field} label=${q.text} value=${values[field.name]}
    onChange=${(v) => setValue(field.name, v)} error=${error} />`;
}

// The subfolder choice as the terminal's picker: a folder's checkbox covers
// its own files; switching one switches the folders below along, unless
// "only this folder" is used. While the caches are used, the files an earlier
// run handled (cached: per folder, for the form's settings) are marked as
// skipped, and a folder with nothing else says so.
function FolderTree({ folder, excluded, setExcluded, cached, useCache }) {
  const entries = folder.tree;
  if (!entries.length) return null;
  const below = (rel) => entries.filter((e) => e.rel.startsWith(rel + '/')).map((e) => e.rel);
  const toggle = (rel, along) => {
    const next = new Set(excluded);
    const on = next.has(rel);
    for (const r of [rel, ...(along ? below(rel) : [])]) on ? next.delete(r) : next.add(r);
    setExcluded(next);
  };
  const counts = (i, v) => [i && `${i} image${i === 1 ? '' : 's'}`, v && `${v} video${v === 1 ? '' : 's'}`]
    .filter(Boolean).join(' · ');
  const skippedOf = (rel) => (useCache ? (rel === '.' ? cached.root : cached.tree[rel]) || [0, 0] : [0, 0]);
  // "2 images · 1 video, 1 done before: skipped" / "(nothing new: done before)"
  const amount = (rel, i, v) => {
    const [si, sv] = skippedOf(rel);
    const skipped = si + sv;
    if (!skipped) return html`<span class="muted">${counts(i, v)}</span>`;
    if (skipped === i + v) return html`<span class="muted">${counts(i, v)}</span> <span class="cached">all done before: skipped</span>`;
    return html`<span class="muted">${counts(i, v)}</span> <span class="cached">${skipped} done before: skipped</span>`;
  };
  const allDone = (rel, i, v) => { const [si, sv] = skippedOf(rel); return i + v > 0 && si + sv === i + v; };
  const total = [cached.root, ...Object.values(cached.tree)].reduce((n, [i, v]) => n + i + v, 0);
  const [ri, rv] = folder.rootCounts;
  return html`<section class="section">
    <h2>Folders</h2>
    <p class="muted small">A folder's checkbox covers its own files. Switching a folder switches the folders below it too.</p>
    ${total > 0 && (useCache
      ? html`<p class="note">${total} file${total === 1 ? ' was' : 's were'} handled by earlier runs and ${total === 1 ? 'is' : 'are'} skipped
          (marked below): the caches keep their result while a file and the settings that decided it stay the same.
          To check them again, choose <i>Scan all files again</i> below.</p>`
      : html`<p class="note">The caches are ignored for this run: the ${total} file${total === 1 ? '' : 's'} earlier runs handled ${total === 1 ? 'is' : 'are'} checked again.</p>`)}
    <ul class="tree">
      <li class=${allDone('.', ri, rv) ? 'done-before' : ''}><label><input type="checkbox" checked=${!excluded.has('.')} onChange=${() => toggle('.', false)} />
        <span class="mono">${baseName(folder.source)}</span> <span class="muted">(directly in it${ri || rv ? ': ' : ''}</span>${ri || rv ? amount('.', ri, rv) : ''}<span class="muted">)</span></label></li>
      ${entries.map((e) => {
        const depth = e.rel.split('/').length;
        const kids = below(e.rel).length;
        return html`<li class=${allDone(e.rel, e.images, e.videos) ? 'done-before' : ''} style=${`padding-left:${depth * 20}px`}>
          <label><input type="checkbox" checked=${!excluded.has(e.rel)} onChange=${() => toggle(e.rel, true)} />
            <span class="mono">${e.rel.split('/').pop()}</span> ${amount(e.rel, e.images, e.videos)}</label>
          ${kids > 0 && html` <button class="link" onClick=${() => toggle(e.rel, false)}>only this folder</button>`}
        </li>`;
      })}
    </ul>
  </section>`;
}

function SetupView({ setup, onSetup, onError }) {
  const folder = setup.folder;
  const fields = {};
  for (const g of setup.schema.groups) for (const f of g.fields) fields[f.name] = f;
  const [values, setValues] = useState(() => Object.fromEntries(Object.values(fields).map((f) => [f.name, f.value])));
  const [source, setSource] = useState(folder.source);
  const [target, setTarget] = useState(folder.target);
  const [excluded, setExcluded] = useState(() => {
    if (values.subfolders === 'exclude') return new Set(folder.tree.map((e) => e.rel).concat(values.exclude_folders));
    return new Set(values.exclude_folders);
  });
  const [mode, setMode] = useState(folder.resume ? (folder.reset ? 'reset' : '') : 'new');
  const [dryRun, setDryRun] = useState(folder.dryRun);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  // What the caches would skip, for the settings as they are in the form now
  // (the target format, qualities, ... decide whether a cached result still holds).
  const [cached, setCached] = useState(folder.cached || { root: [0, 0], tree: {} });
  useEffect(() => { setCached(folder.cached || { root: [0, 0], tree: {} }); }, [folder]);
  const firstValues = useRef(true);
  useEffect(() => {
    if (firstValues.current) { firstValues.current = false; return undefined; }
    if (!values.use_cache || folder.error) return undefined;
    let alive = true;
    const timer = setTimeout(async () => {
      try { const data = await api('/cached', { settings: values }); if (alive) setCached(data); } catch (err) { /* keep the last counts */ }
    }, 300);
    return () => { alive = false; clearTimeout(timer); };
  }, [values]);

  const setValue = (name, value) => setValues((v) => ({ ...v, [name]: value }));
  const hasTarget = !!folder.target;
  const useFolder = async () => {
    setBusy(true);
    try { onSetup(await api('/folder', { source, target })); onError(''); } catch (err) { onError(err.message); }
    setBusy(false);
  };
  const start = async () => {
    setBusy(true);
    setErrors({});
    const settings = { ...values, subfolders: 'include', exclude_folders: [...excluded].sort() };
    try {
      await api('/start', { settings, mode, dry_run: dryRun });
      onError('');
    } catch (err) {
      if (err.fields) setErrors(err.fields);
      onError(err.message);
    }
    setBusy(false);
  };
  const resumeLocked = mode === 'resume';
  const ready = !folder.error && !folder.busy && mode !== '';
  // A resumed run keeps its own target and settings, whatever the form says.
  const runTarget = resumeLocked ? folder.resume.output || '' : folder.target;
  const deleteSetting = resumeLocked ? folder.resume.changed.delete_original ?? null : values.delete_original;
  const deleting = deleteSetting === null ? !runTarget : deleteSetting;
  return html`
    <section class="section">
      <h2>Folder</h2>
      <div class="field"><label for="source">Convert the files in</label>
        <input type="text" id="source" class="mono" value=${source} onInput=${(e) => setSource(e.target.value)} /></div>
      <div class="field"><label for="target">Write the results to</label>
        <input type="text" id="target" class="mono" placeholder="the same folder (in place)" value=${target}
          onInput=${(e) => setTarget(e.target.value)} /></div>
      <div class="row">
        <button class="btn" disabled=${busy || (source === folder.source && target === folder.target)} onClick=${useFolder}>Use these folders</button>
      </div>
      ${folder.error && html`<p class="error">${folder.error}</p>`}
      ${folder.busy && html`<p class="error">${folder.busy}</p>`}
      ${!folder.error && html`<p class="muted">${resumeLocked ? `The interrupted run continues ${runTarget ? `into ${runTarget}` : 'in place'}. ` : ''}${runTarget
        ? 'With a target folder the source is never changed.'
        : deleting ? 'In place: originals go to the trash once their result is verified.'
                   : 'In place: results are written next to the originals, which are kept.'}</p>`}
      ${folder.notes.map((n) => html`<p class="note">${n}</p>`)}
    </section>
    ${folder.resume && html`<section class="section resume">
      <h2>An interrupted run of this folder</h2>
      <p class="muted">Finished stages: ${Object.entries(folder.resume.done).filter(([, d]) => d).map(([s]) => s).join(', ') || 'none yet'}.
        ${Object.keys(folder.resume.changed).length
          ? ` Its settings differ from the defaults in: ${Object.entries(folder.resume.changed).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(', ')}.`
          : ' It used the default settings.'}</p>
      <div class="row">
        <label class="choice"><input type="radio" name="mode" checked=${mode === 'resume'} onChange=${() => setMode('resume')} /> Resume with its saved settings</label>
        <label class="choice"><input type="radio" name="mode" checked=${mode === 'reset'} onChange=${() => setMode('reset')} /> Start over with the settings below</label>
      </div>
    </section>`}
    ${!folder.error && !resumeLocked && html`<${FolderTree} folder=${folder} excluded=${excluded} setExcluded=${setExcluded}
      cached=${cached} useCache=${values.use_cache} />`}
    <fieldset class="plain" disabled=${resumeLocked}>
      ${setup.schema.questionnaire.map((sec) => html`<section class="section">
        <h2>${sec.title}</h2>
        ${sec.questions.filter((q) => shown(q.when, values, hasTarget)).map((q) => html`<${Question} key=${q.setting}
          q=${q} field=${fields[q.setting]} values=${values} setValue=${setValue} hasTarget=${hasTarget}
          error=${errors[q.setting]} />`)}
      </section>`)}
      <details class="section all">
        <summary><h2>All settings</h2></summary>
        ${setup.schema.groups.map((g) => html`<div class="group">
          <h3>${g.title}</h3>
          ${g.fields.filter((f) => !['subfolders', 'exclude_folders'].includes(f.name)).map((f) => html`<${Field}
            key=${f.name} field=${f} value=${values[f.name]} onChange=${(v) => setValue(f.name, v)} error=${errors[f.name]} />`)}
        </div>`)}
      </details>
    </fieldset>
    <section class="section start">
      <label class="check"><input type="checkbox" checked=${dryRun} onChange=${(e) => setDryRun(e.target.checked)} />
        Preview only (dry run): show what would happen, change nothing</label>
      ${folder.resume && mode === '' && html`<p class="note">Choose above whether to resume or start over.</p>`}
      <div class="row"><button class="btn btn-primary" disabled=${busy || !ready} onClick=${start}>
        ${dryRun ? 'Start the preview' : mode === 'resume' ? 'Resume the run' : 'Start'}</button></div>
    </section>`;
}

// --- the run ----------------------------------------------------------------------
// As keepbest's page: the run reads from top to bottom like the terminal, one
// section per stage, then the summary and what to do next. A finished stage
// folds its files away and keeps only what needs a look in view, so the end
// of a long run is near its summary.
const OUTCOME_LABELS = { converted: 'converted', skipped: 'skipped', cached: 'known from earlier runs', failed: 'failed',
                         discarded: 'discarded (larger)', conflict: 'name conflict', dry: 'would be converted',
                         finished: 'finished' };
const SHOWN_MESSAGES = 5;
const RUNNING_FILES = 6;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function FileList({ files }) {
  return html`<ul class="files">${files.map((f, i) => html`<li key=${i}
    class=${f.status === 'OK' || f.status === 'SALVAGED' ? '' : 'other'}>
    <span class="tag">${f.status}</span>
    <span class="path">${baseName(f.src)}${f.dest ? html` → ${f.dest}` : ''}</span>
    <span class="muted small">${saving(f.before, f.after)}</span></li>`)}</ul>`;
}

// The first few in view, the rest one click away.
function Folded({ items, shown, more, render: draw }) {
  if (items.length <= shown) return draw(items);
  return html`${draw(items.slice(0, shown))}
    <details><summary class="small muted">${more(items.length - shown)}</summary>${draw(items.slice(shown))}</details>`;
}

function Messages({ messages }) {
  return html`<${Folded} items=${messages} shown=${SHOWN_MESSAGES} more=${(n) => `${plural(n, 'more message')}`}
    render=${(list) => list.map((m) => html`<p class=${'msg ' + m.level}><span class="tag">${m.tag}</span> ${m.text}</p>`)} />`;
}

function StageCard({ stage, encodes, messages, current }) {
  const running = stage.state === 'running';
  const pct = stage.total ? Math.min(100, Math.round(stage.done * 100 / stage.total)) : null;
  const state = { waiting: 'waiting', running: 'running', done: stage.code === 0 ? 'done' : `ended with code ${stage.code}`,
                  earlier: 'done in the interrupted run' }[stage.state];
  const old = stage.state === 'done' && !current;
  const files = stage.files || [];
  return html`<section class=${'section stage ' + stage.state + (stage.code ? ' bad' : '') + (old ? ' old' : '')}>
    <div class="row between"><h2>${stage.title}</h2><span class="muted small">${running
      ? html`<span class="spinner" aria-hidden="true"></span> ` : ''}${state}</span></div>
    ${running && stage.total !== null && html`<div class="bar" role="progressbar" aria-valuenow=${pct || 0}
      aria-valuemin="0" aria-valuemax="100"><div style=${`width:${pct || 0}%`}></div></div>
      <p class="small muted">${stage.done} of ${plural(stage.total, 'file')}</p>`}
    ${stage.state === 'done' && stage.done > 0 && html`<p class="small muted">${plural(stage.done, 'file')}${stage.elapsed >= 1
      ? ` in ${duration(Math.round(stage.elapsed))}` : ''}</p>`}
    ${Object.keys(stage.outcomes).length > 0 && html`<p class="small">${Object.entries(stage.outcomes)
      .map(([k, n]) => `${n} ${OUTCOME_LABELS[k] || k}`).join(' · ')}</p>`}
    ${stage.before > 0 && html`<p class="small">${saving(stage.before, stage.after)}</p>`}
    ${encodes.map((e) => html`<div class="encode">
      <span class="mono small">#${e.idx}${e.total ? '/' + e.total : ''} ${e.encoder.toUpperCase()}</span>
      <span class="path small">${baseName(e.src)}</span>
      <div class="bar thin"><div style=${`width:${e.percent || 0}%`}></div></div>
      <span class="small muted">${e.percent !== null ? Math.round(e.percent) + '%' : ''} ${e.speed || ''} ${e.eta !== null ? duration(e.eta) + ' left' : ''}</span>
    </div>`)}
    ${messages.length > 0 && html`<${Messages} messages=${messages} />`}
    ${running && files.length > 0 && html`<${FileList} files=${files.slice(-RUNNING_FILES)} />`}
    ${!running && files.length > 0 && html`<details><summary class="small muted">${stage.listed > files.length
      ? `Files: the last ${files.length} of ${stage.listed} (all are in the log)` : `Files (${files.length})`}</summary>
      <${FileList} files=${files} /></details>`}
  </section>`;
}

// Whether any stage got going: a run that could not start has no numbers.
const ran = (progress) => !!progress && progress.stages.some((s) => s.state === 'running' || s.state === 'done');

// The run's numbers in tiles, as keepbest's summary.
function Summary({ progress, code }) {
  const t = progress.totals;
  const sum = (key) => progress.stages.reduce((n, s) => n + (s.outcomes[key] || 0), 0);
  const elapsed = progress.stages.reduce((n, s) => n + (s.elapsed || 0), 0);
  const dry = progress.dryRun;
  const tiles = [[dry ? 'Would be converted' : 'Converted', dry ? sum('dry') : t.converted]];
  if (t.before > 0) {
    tiles.push([t.after <= t.before ? 'Space saved' : 'Space added', formatBytes(Math.abs(t.before - t.after)),
                saving(t.before, t.after)]);
  }
  const skipped = sum('skipped') + sum('cached');
  tiles.push(['Skipped', skipped]);
  if (sum('discarded')) tiles.push(['Kept: result larger', sum('discarded')]);
  if (sum('conflict')) tiles.push(['Name conflicts', sum('conflict')]);
  tiles.push(['Failed', t.failed]);
  if (elapsed >= 1) tiles.push(['Time', duration(Math.round(elapsed))]);
  return html`<section class="section">
    <h2>${dry ? 'Summary – preview' : code === 130 ? 'Summary – until it was stopped' : 'Summary'}</h2>
    <dl class="tiles">${tiles.map(([label, value, sub]) => html`<div class="tile" key=${label}><dt>${label}</dt><dd>${value}</dd>
      ${sub && html`<dd class="sub">${sub}</dd>`}</div>`)}</dl>
    ${sum('cached') > 0 && html`<p class="small muted">${plural(sum('cached'), 'file')} of the skipped ${sum('cached') === 1
      ? 'was' : 'were'} handled by earlier runs.</p>`}
    ${t.failed > 0 && html`<p class="small muted">The originals of the failed files are kept; the log tab says why.</p>`}
    ${dry && html`<p class="small muted">Nothing was written or deleted.</p>`}
  </section>`;
}

function LogView({ lines, partial }) {
  const box = useRef(null);
  const stick = useRef(true);
  const own = useRef(false);
  useEffect(() => {
    const el = box.current;
    if (el && stick.current) { own.current = true; el.scrollTop = el.scrollHeight; }
  });
  const onScroll = () => {
    const el = box.current;
    if (own.current) { own.current = false; return; }
    stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  };
  return html`<div class="log mono" ref=${box} onScroll=${onScroll}>
    ${lines.map((l, i) => html`<div key=${i}><${AnsiLine} text=${l} /></div>`)}
    ${partial && html`<div class="partial"><${AnsiLine} text=${partial} /></div>`}
  </div>`;
}

function RunView({ state, log, tab }) {
  const p = state.progress;
  const running = state.phase === 'running';
  const early = p.messages.filter((m) => !m.stage);
  return html`
    ${p.dryRun && html`<p class="note">Preview only: nothing is written or deleted.</p>`}
    ${running && state.background && html`<p class="note">Keeps running when you close this tab. A desktop
      notification says when it is done; its "Open" button brings this page back.</p>`}
    ${tab === 'log' ? html`<${LogView} lines=${log.lines} partial=${log.partial} />` : html`
      ${early.length > 0 && html`<section class="section"><h2>Before the stages</h2><${Messages} messages=${early} /></section>`}
      ${p.stages.map((s) => html`<${StageCard} key=${s.stage} stage=${s} current=${s.stage === p.current && running}
        encodes=${s.stage === 'video' ? p.encodes : []} messages=${p.messages.filter((m) => m.stage === s.stage)} />`)}
      ${state.phase === 'finished' && ran(p) && html`<${Summary} progress=${p} code=${state.code} />`}`}`;
}

// The stages as steps in the top bar, as keepbest's.
function Steps({ state }) {
  const p = state.progress;
  const finished = state.phase === 'finished';
  const steps = p.stages.map((s) => [s.stage, s.title,
    s.state === 'running' && !finished ? 'now' : s.state === 'done' || s.state === 'earlier' ? 'done' : '']);
  steps.push(['summary', 'Summary', finished ? 'now' : '']);
  return html`<ol class="steps" aria-label="Progress">${steps.map(([id, name, cls]) => html`<li key=${id}
    class=${'step ' + cls} aria-current=${cls === 'now' ? 'step' : undefined}>${name}</li>`)}</ol>`;
}

const ENDINGS = { 0: 'The run has finished.', 1: 'The run could not start.', 2: 'A setting was not valid.',
                  3: 'The run has finished, but some files failed; their originals are kept.',
                  130: 'The run was stopped. Its progress is saved: the next start offers to resume it.',
                  255: 'The run stopped: an encoder vanished.' };

function Finished({ state, onAgain, onClose }) {
  return html`<section class="section finished">
    <h2>${ENDINGS[state.code] || `The run ended with code ${state.code}.`}</h2>
    ${state.returnUrl && ran(state.progress) && html`<p class="muted">${state.returnName} shows these results next to its own.</p>`}
    <div class="row">
      ${!state.returnUrl && html`<button class="btn" onClick=${onAgain}>Process another folder</button>`}
      <button class="btn btn-primary" onClick=${onClose}>${state.returnUrl ? `Back to ${state.returnName}` : 'Close'}</button>
    </div>
  </section>`;
}

// --- the page ---------------------------------------------------------------------
// A newer media-optimizer, in one line under the top bar: the command, and with
// pipx a button that installs it; then the program starts again on the same
// address and this page loads itself again.
function UpdateNotice({ update, onError }) {
  const [hidden, setHidden] = useState(false);
  const [sending, setSending] = useState(false);
  if (!update || (hidden && update.state === 'available')) return null;
  const { version, state } = update;
  const install = async () => {
    setSending(true);
    try { await api('/update', {}); } catch (err) { onError(err.message); }
    setSending(false);
  };
  if (state === 'installing' || state === 'restarting') {
    return html`<div class="update" role="status">${state === 'installing'
      ? `Installing media-optimizer ${version}; this takes a minute or two, then it starts again.`
      : `media-optimizer ${version} is installed and starts again…`}</div>`;
  }
  const failed = state === 'failed';
  const where = 'Possible when no run is going on';
  return html`<div class="update" role="status">
    <div class="row">
      ${failed
        ? html`<span class="update-bad">The update to media-optimizer ${version} didn't work (exit code ${update.code}).</span>`
        : html`<span><b>media-optimizer ${version} is available</b> <span class="muted">(this is ${update.current})</span></span>`}
      ${update.installable && html`<button class="btn btn-small" disabled=${!update.ready || sending}
        title=${update.ready ? 'Install it with pipx, then media-optimizer starts again here' : where}
        onClick=${install}>${failed ? 'Try again' : 'Update'}</button>`}
      <a class="small" href=${update.notes} target="_blank" rel="noopener noreferrer">What's new</a>
      ${!failed && html`<button class="btn btn-small" onClick=${() => setHidden(true)}>Not now</button>`}
      ${update.installable && !update.ready && html`<span class="small muted">Update: when the run has finished</span>`}
    </div>
    ${failed && html`<pre class="update-command">${update.output}</pre>
      <p class="small muted">All of pipx's output is in update.log in the state folder.</p>`}
    <details class="small" open=${failed}>
      <summary>${update.installable ? 'The command, to run it in a terminal' : 'How to update'}</summary>
      <pre class="update-command">${update.command}</pre>
    </details>
  </div>`;
}

function App() {
  const [state, setState] = useState(null);
  const [setup, setSetup] = useState(null);
  const [log, setLog] = useState({ lines: [], partial: '', next: 0, run: 0 });
  const [error, setError] = useState('');
  const [closed, setClosed] = useState(false);
  const [tab, setTab] = useState('progress');
  const logRef = useRef(log);
  const stick = useRef(true);
  const lastHeight = useRef(0);
  const ownScroll = useRef(false);
  const phaseRef = useRef(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      let rev = 0;
      let boot = null;
      let restarting = false;
      while (alive) {
        try {
          const known = logRef.current;
          // Updated: the old program goes away, the new one answers on the same address.
          const next = await api(`/state?since=${restarting ? 0 : rev}&log=${known.next}&run=${known.run}`);
          if (boot && next.boot !== boot) { location.reload(); return; }
          boot = next.boot;
          rev = next.rev;
          restarting = !!next.update && next.update.state === 'restarting';
          if (next.log) {
            // The server sends the lines from where this page stopped, or a
            // new run's from its start.
            const base = next.run === known.run ? known.lines : [];
            const fresh = { lines: base.concat(next.log.lines).slice(-4000), partial: next.log.partial,
                            next: next.log.from + next.log.lines.length, run: next.run };
            logRef.current = fresh;
            setLog(fresh);
          }
          setState(next);
          setError('');
          if (restarting) await sleep(300);
          else if (next.phase === 'finished') await sleep(2000);
        } catch (err) {
          if (restarting) { await sleep(300); continue; }
          setError(err.message);
          await sleep(2000);
        }
      }
    })();
    const ping = setInterval(() => api('/ping', {}).catch(() => {}), 15000);
    const closing = () => navigator.sendBeacon('/api/closing', '');
    addEventListener('pagehide', closing);
    return () => { alive = false; clearInterval(ping); removeEventListener('pagehide', closing); };
  }, []);

  // The form, fresh whenever the page is back at the start.
  const phase = state && state.phase;
  useEffect(() => {
    if (phase === 'setup') api('/setup').then(setSetup).catch((err) => setError(err.message));
  }, [phase]);

  // While a run goes on, the browser asks before the tab closes (closing it
  // stops the run), unless it was asked to keep running in the background.
  const background = !!(state && state.background);
  useEffect(() => {
    if (phase !== 'running' || background) return undefined;
    const guard = (event) => event.preventDefault();
    addEventListener('beforeunload', guard);
    return () => removeEventListener('beforeunload', guard);
  }, [phase, background]);

  // As keepbest's page: new content scrolls the page only for a reader who
  // was at its end; one who scrolled up to read stays where they are.
  const follow = () => {
    if (phaseRef.current === 'setup') return;
    const height = document.documentElement.scrollHeight;
    if (height !== lastHeight.current && stick.current) {
      ownScroll.current = true;
      scrollTo(0, height);
    }
    lastHeight.current = height;
  };
  useEffect(() => {
    const onScroll = () => {
      if (ownScroll.current) { ownScroll.current = false; return; }
      stick.current = scrollY + innerHeight >= document.documentElement.scrollHeight - 40;
    };
    addEventListener('scroll', onScroll, { passive: true });
    const grows = new ResizeObserver(follow);
    grows.observe(document.body);
    return () => { removeEventListener('scroll', onScroll); grows.disconnect(); };
  }, []);
  // The form starts at its top; a run is followed from its start.
  phaseRef.current = phase;
  useEffect(() => { stick.current = phase !== 'setup'; if (phase === 'setup') setTab('progress'); }, [phase]);
  useEffect(follow);

  const stop = async () => {
    if (!confirm('Stop the run? Its progress is saved; the next start offers to resume it.')) return;
    try { await api('/stop', {}); } catch (err) { setError(err.message); }
  };
  const keepRunning = async () => {
    try { await api('/background', {}); } catch (err) { setError(err.message); }
  };
  const again = async () => { try { await api('/again', {}); } catch (err) { setError(err.message); } };
  const close = async () => {
    try { await api('/quit', {}); } catch (err) { setError(err.message); return; }
    if (state.returnUrl) location.href = state.returnUrl;
    else setClosed(true);
  };

  if (!state) return html`<main><p class="muted">Connecting…</p></main>`;
  const running = phase === 'running';
  return html`
    <div class="top"><header class="topbar">
      <span class="brand"><img src="/static/icon.svg" alt="" width="26" height="26" /> media-optimizer
        <span class="version">${state.version}</span></span>
      ${phase !== 'setup' && state.progress && html`<${Steps} state=${state} />`}
      <span class="spacer"></span>
      ${phase !== 'setup' && state.progress && html`<div class="segmented" role="tablist">
        <button role="tab" aria-selected=${tab === 'progress'} class=${tab === 'progress' ? 'on' : ''} onClick=${() => setTab('progress')}>Progress</button>
        <button role="tab" aria-selected=${tab === 'log'} class=${tab === 'log' ? 'on' : ''} onClick=${() => setTab('log')}>Log</button>
      </div>`}
      ${running && !state.background && html`<button class="btn btn-small" disabled=${state.stopping}
        onClick=${keepRunning}>Keep running in the background</button>`}
      ${running && html`<button class="btn btn-small btn-danger" disabled=${state.stopping} onClick=${stop}>
        ${state.stopping ? 'Stopping…' : 'Stop'}</button>`}
      ${phase === 'setup' && html`<button class="btn btn-small" onClick=${close}>${state.returnUrl ? `Back to ${state.returnName}` : 'Close'}</button>`}
    </header>
    <${UpdateNotice} update=${state.update} onError=${setError} /></div>
    <main>
      ${phase === 'setup' && (setup ? html`<${SetupView} key=${setup.folder.source + '|' + setup.folder.target}
        setup=${setup} onSetup=${setSetup} onError=${setError} />` : html`<p class="muted">Looking at the folder…</p>`)}
      ${phase !== 'setup' && state.progress && html`<${RunView} state=${state} log=${log} tab=${tab} />`}
      ${phase === 'finished' && html`<${Finished} state=${state} onAgain=${again} onClose=${close} />`}
    </main>
    ${error && !closed && html`<div class="toast" role="alert">${error}</div>`}
    ${closed && html`<div class="overlay"><div class="section"><h2>media-optimizer has ended</h2>
      <p class="muted">You can close this tab.</p></div></div>`}
  `;
}

render(html`<${App} />`, document.getElementById('app'));
