// LLM Shadow Proxy — Test Console
//
// - Sends a POST /v1/chat to the local proxy
// - Immediately renders the primary response returned by the endpoint
// - Then polls GET /v1/evaluations/{request_id} every 500ms until the
//   candidate finishes (or errors / goes stale), and updates the badges.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const els = {
  chat:        $('#chat'),
  userInput:   $('#userInput'),
  systemPrompt:$('#systemPrompt'),
  sendBtn:     $('#sendBtn'),
  clearBtn:    $('#clearBtn'),
  apiKey:      $('#apiKey'),
  statusLine:  $('#statusLine'),
  recent:      $('#recent'),
  refreshBtn:  $('#refreshBtn'),
  msgTemplate: $('#msg-template'),
  statPrimary: $('#stat-primary'),
  statCandidate: $('#stat-candidate'),
  statMatch:   $('#stat-match'),
  statTotal:   $('#stat-total'),
};

// Restore stashed inputs from localStorage
if (localStorage.getItem('systemPrompt')) els.systemPrompt.value = localStorage.getItem('systemPrompt');
if (localStorage.getItem('apiKey'))       els.apiKey.value       = localStorage.getItem('apiKey');
els.systemPrompt.addEventListener('input', () => localStorage.setItem('systemPrompt', els.systemPrompt.value));
els.apiKey.addEventListener('input',       () => localStorage.setItem('apiKey', els.apiKey.value));

function authHeaders() {
  const key = els.apiKey.value.trim();
  return key ? { 'Authorization': `Bearer ${key}` } : {};
}

function setStatus(text, cls = '') {
  els.statusLine.textContent = text || '';
  els.statusLine.className = cls;
}

function renderMessage({ role, content, requestId }) {
  const frag = els.msgTemplate.content.cloneNode(true);
  const msg = frag.querySelector('.msg');
  msg.classList.add(`role-${role}`);
  frag.querySelector('.role').textContent = role;
  frag.querySelector('.req-id').textContent = requestId ? `#${requestId.slice(0, 12)}…` : '';
  frag.querySelector('.msg-body').textContent = content;
  els.chat.appendChild(frag);
  els.chat.scrollTop = els.chat.scrollHeight;
  return els.chat.lastElementChild;
}

function attachPrimaryRawIfAvailable(msgEl, rawJson) {
  if (!rawJson) return;
  const details = msgEl.querySelector('.primary-raw-detail');
  const pre = msgEl.querySelector('.primary-raw');
  try {
    pre.textContent = JSON.stringify(rawJson, null, 2);
  } catch {
    pre.textContent = String(rawJson);
  }
  details.hidden = false;
}

function addBadge(msgEl, label, cls) {
  const badges = msgEl.querySelector('.badges');
  const span = document.createElement('span');
  span.className = `badge ${cls}`;
  span.innerHTML = `<span class="dot"></span>${label}`;
  badges.appendChild(span);
  return span;
}

function clearBadges(msgEl) {
  msgEl.querySelector('.badges').innerHTML = '';
}

function verdictBadgeClass(v) {
  return {
    match: 'match',
    mismatch: 'mismatch',
    invalid_json: 'invalid_json',
    candidate_error: 'candidate_error',
    primary_error: 'candidate_error',
    pending: 'candidate-pending',
  }[v] || 'candidate-pending';
}

async function send() {
  const userText = els.userInput.value.trim();
  if (!userText) return;
  const systemText = els.systemPrompt.value.trim();

  els.sendBtn.disabled = true;
  setStatus('sending…');

  renderMessage({ role: 'user', content: userText });
  els.userInput.value = '';

  const body = {
    messages: [
      ...(systemText ? [{ role: 'system', content: systemText }] : []),
      { role: 'user', content: userText },
    ],
    temperature: 0.0,
    // The gpt-oss-* models are reasoning models: they consume tokens on an
    // internal ``reasoning_content`` field before emitting the visible
    // ``content``. 256 is not enough — the answer gets truncated with
    // finish_reason=length. 1024 is a comfortable default for JSON routing
    // prompts; users can lower it if they know their model isn't a reasoner.
    max_completion_tokens: 1024,
  };

  let response;
  try {
    const t0 = performance.now();
    const r = await fetch('/v1/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(body),
    });
    const clientLatency = Math.round(performance.now() - t0);
    if (!r.ok) {
      const errText = await r.text();
      const errMsg = renderMessage({
        role: 'error',
        content: `HTTP ${r.status}: ${errText}`,
      });
      addBadge(errMsg, `client ${clientLatency}ms`, 'primary-err');
      setStatus(`error ${r.status}`, 'text-err');
      return;
    }
    response = await r.json();
    setStatus(`primary ok · client ${clientLatency}ms`, 'text-ok');
  } catch (e) {
    const errMsg = renderMessage({ role: 'error', content: `Network error: ${e.message}` });
    addBadge(errMsg, 'network', 'primary-err');
    setStatus('network error', 'text-err');
    return;
  } finally {
    els.sendBtn.disabled = false;
  }

  const requestId = response.request_id;
  const primaryModel = response.primary_model;
  const primaryContent = response.response?.choices?.[0]?.message?.content ?? '(no content)';

  const assistantMsg = renderMessage({
    role: 'assistant',
    content: primaryContent,
    requestId,
  });
  addBadge(assistantMsg, `Primary · ${primaryModel}`, 'primary-ok');
  const candBadge = addBadge(assistantMsg, 'Candidate · pending…', 'candidate-pending');
  attachPrimaryRawIfAvailable(assistantMsg, response.response);

  // Stash the candidate model on the DOM node so we can label the block
  // even before the raw payload comes back.
  assistantMsg.dataset.requestId = requestId;

  pollCandidate(requestId, assistantMsg, candBadge).then(() => {
    refreshRecent();
    refreshStats();
  });
}

async function pollCandidate(requestId, msgEl, pendingBadge) {
  const deadline = Date.now() + 60_000; // give up after 60s in the UI
  while (Date.now() < deadline) {
    await sleep(500);
    let rec;
    try {
      const r = await fetch(`/v1/evaluations/${encodeURIComponent(requestId)}`, {
        headers: authHeaders(),
      });
      if (r.status === 404) continue;
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      rec = await r.json();
    } catch {
      continue;
    }
    if (rec.candidate_status === 'pending' || rec.candidate_status === 'in_progress') {
      pendingBadge.querySelector('.dot')?.classList.add('pulse');
      continue;
    }
    finalizeCandidate(msgEl, rec);
    return;
  }
  pendingBadge.textContent = 'Candidate · timeout (UI wait)';
  pendingBadge.className = 'badge timeout';
}

async function finalizeCandidate(msgEl, rec) {
  clearBadges(msgEl);
  addBadge(msgEl,
    `Primary · ${rec.primary_model}${rec.primary_latency_ms != null ? ` · ${rec.primary_latency_ms}ms` : ''}`,
    'primary-ok');

  const verdict = rec.verdict;
  const verdictLabel = {
    match: 'MATCH',
    mismatch: 'MISMATCH',
    invalid_json: 'INVALID JSON',
    candidate_error: 'CANDIDATE ERROR',
    primary_error: 'PRIMARY ERROR',
  }[verdict] || verdict.toUpperCase();

  addBadge(msgEl,
    `${verdictLabel} · ${rec.candidate_model}${rec.candidate_latency_ms != null ? ` · ${rec.candidate_latency_ms}ms` : ''}`,
    verdictBadgeClass(verdict));

  if (rec.candidate_action || rec.primary_action) {
    addBadge(msgEl,
      `action: ${rec.primary_action ?? '∅'} → ${rec.candidate_action ?? '∅'}`,
      verdict === 'match' ? 'match' : 'mismatch');
  }

  // Reveal the candidate block and prime it with what we already know from
  // the finalized record. Then try to hydrate the full response text and
  // raw JSON from the /raw endpoint.
  const block = msgEl.querySelector('.candidate-block');
  const body = msgEl.querySelector('.candidate-body');
  const modelSpan = msgEl.querySelector('.candidate-model');
  const reasons = msgEl.querySelector('.reasons');
  block.hidden = false;
  block.classList.remove('match', 'mismatch', 'invalid_json', 'candidate_error');
  block.classList.add(verdictBadgeClass(verdict));
  modelSpan.textContent = rec.candidate_model || '';
  reasons.textContent = 'reasons: ' + (rec.reasons?.join(' · ') || '—');

  if (rec.candidate_error) {
    body.classList.add('is-error');
    body.textContent = rec.candidate_error;
  } else if (rec.candidate_action) {
    body.classList.remove('is-error');
    body.textContent = `{"action": ${JSON.stringify(rec.candidate_action)}}`;
  } else {
    body.classList.remove('is-error');
    body.textContent = '(candidate returned no content)';
  }

  await hydrateRawResponses(msgEl, rec.request_id);
}

async function hydrateRawResponses(msgEl, requestId) {
  let raw;
  try {
    const r = await fetch(`/v1/evaluations/${encodeURIComponent(requestId)}/raw`, {
      headers: authHeaders(),
    });
    if (!r.ok) return;
    raw = await r.json();
  } catch {
    return;
  }
  if (!raw) return;

  if (raw.primary_response) {
    attachPrimaryRawIfAvailable(msgEl, raw.primary_response);
  }

  const body = msgEl.querySelector('.candidate-body');
  const isErrorBody = body.classList.contains('is-error');
  if (raw.candidate_content && !isErrorBody) {
    body.textContent = raw.candidate_content;
  }

  if (raw.candidate_response) {
    const details = msgEl.querySelector('.candidate-raw-detail');
    const pre = msgEl.querySelector('.candidate-raw');
    try {
      pre.textContent = JSON.stringify(raw.candidate_response, null, 2);
    } catch {
      pre.textContent = String(raw.candidate_response);
    }
    details.hidden = false;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function refreshStats() {
  try {
    const [sumR, cfgR] = await Promise.all([
      fetch('/v1/evaluations/summary?window_seconds=86400', { headers: authHeaders() }),
      fetch('/v1/config', { headers: authHeaders() }),
    ]);
    if (cfgR.ok) {
      const cfg = await cfgR.json();
      const rt = cfg.routes?.default;
      if (rt) {
        els.statPrimary.textContent = rt.primary?.model_id ?? '—';
        els.statCandidate.textContent = rt.candidate?.model_id ?? '—';
      }
    }
    if (sumR.ok) {
      const s = await sumR.json();
      els.statMatch.textContent = s.total ? `${(100 * s.match_rate).toFixed(1)}%` : '—';
      els.statTotal.textContent = String(s.total);
    }
  } catch { /* ignore */ }
}

async function refreshRecent() {
  try {
    const r = await fetch('/v1/evaluations?limit=10', { headers: authHeaders() });
    if (!r.ok) return;
    const rows = await r.json();
    if (!rows.length) {
      els.recent.innerHTML = '<div class="empty">No evaluations yet.</div>';
      return;
    }
    els.recent.innerHTML = '';
    for (const row of rows) {
      const div = document.createElement('div');
      div.className = 'recent';
      const verdictCls = ({
        match: 'text-ok',
        mismatch: 'text-mismatch',
        invalid_json: 'text-warn',
        candidate_error: 'text-err',
        primary_error: 'text-err',
        pending: 'text-dim',
      })[row.verdict] || 'text-dim';
      div.innerHTML = `
        <div class="recent-top">
          <span class="recent-id">${row.request_id.slice(0,14)}…</span>
          <span class="recent-verdict ${verdictCls}">${row.verdict}</span>
        </div>
        <div class="action">${row.primary_action ?? '∅'} → ${row.candidate_action ?? '∅'}</div>
        <div class="lats">p:${row.primary_latency_ms ?? '—'}ms · c:${row.candidate_latency_ms ?? '—'}ms</div>
      `;
      els.recent.appendChild(div);
    }
  } catch { /* ignore */ }
}

els.sendBtn.addEventListener('click', send);
els.clearBtn.addEventListener('click', () => { els.chat.innerHTML = ''; });
els.refreshBtn.addEventListener('click', () => { refreshRecent(); refreshStats(); });
els.userInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

// ---------------------------------------------------------------------------
//  Live metrics panel  →  polls GET /v1/metrics every 3s
// ---------------------------------------------------------------------------
const METRIC_CARDS = [
  { key: 'requests_total',       label: 'Requests total',     fmt: v => v },
  { key: 'requests_success',     label: 'Successful',         fmt: v => v },
  { key: 'shadow_enqueued',      label: 'Shadow enqueued',    fmt: v => v },
  { key: 'shadow_sampled_out',   label: 'Sampled out',        fmt: v => v },
  { key: 'shadow_errors',        label: 'Shadow errors',      fmt: v => v, danger: v => v > 0 },
  { key: 'shadow_timeouts',      label: 'Shadow timeouts',    fmt: v => v, danger: v => v > 0 },
  { key: 'verdict_match',        label: 'Verdict: match',     fmt: v => v, ok: v => v > 0 },
  { key: 'verdict_mismatch',     label: 'Verdict: mismatch',  fmt: v => v, warn: v => v > 0 },
  { key: 'verdict_invalid_json', label: 'Verdict: invalid',   fmt: v => v, warn: v => v > 0 },
  { key: 'exact_match_rate_pct', label: 'Exact match %',      fmt: v => `${v.toFixed(1)}%` },
  { key: 'shadow_sample_rate',   label: 'Sample rate',        fmt: v => `${(100*v).toFixed(0)}%` },
];

function renderMetricsSkeleton() {
  const el = document.getElementById('metrics-grid');
  el.innerHTML = METRIC_CARDS.map(c =>
    `<div class="metric-card" data-key="${c.key}">
       <div class="metric-label">${c.label}</div>
       <div class="metric-value mono">—</div>
     </div>`
  ).join('');
}

async function refreshLiveMetrics() {
  try {
    const r = await fetch('/v1/metrics', { headers: authHeaders() });
    if (!r.ok) return;
    const data = await r.json();
    for (const c of METRIC_CARDS) {
      const card = document.querySelector(`.metric-card[data-key="${c.key}"]`);
      if (!card) continue;
      const val = data[c.key];
      card.querySelector('.metric-value').textContent = c.fmt(val);
      card.classList.toggle('is-danger', !!(c.danger && c.danger(val)));
      card.classList.toggle('is-warn',   !!(c.warn   && c.warn(val)));
      card.classList.toggle('is-ok',     !!(c.ok     && c.ok(val)));
    }
    document.getElementById('metrics-tick').textContent =
      new Date().toLocaleTimeString();
    // Keep the sample-rate slider in sync with server state
    const rate = data.shadow_sample_rate;
    if (typeof rate === 'number' && !document.activeElement.matches('#sample-rate')) {
      const slider = document.getElementById('sample-rate');
      slider.value = Math.round(rate * 100);
      document.getElementById('sample-rate-value').textContent =
        `${Math.round(rate * 100)}%`;
    }
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
//  Sample-rate slider  →  PUT /v1/config
// ---------------------------------------------------------------------------
function initSampleRateControl() {
  const slider = document.getElementById('sample-rate');
  const label  = document.getElementById('sample-rate-value');
  const status = document.getElementById('sample-rate-status');
  const apply  = document.getElementById('sample-rate-apply');

  slider.addEventListener('input', () => {
    label.textContent = `${slider.value}%`;
  });
  apply.addEventListener('click', async () => {
    const rate = Number(slider.value) / 100;
    status.textContent = 'applying…';
    try {
      const r = await fetch('/v1/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ shadow_sample_rate: rate }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      status.textContent = `applied · server sample_rate = ${j.current_shadow_sample_rate}`;
      status.className = 'hint text-ok';
    } catch (e) {
      status.textContent = `failed: ${e.message}`;
      status.className = 'hint text-err';
    }
  });
}

// ---------------------------------------------------------------------------
//  API Explorer  →  a card per endpoint with "Try it" button
// ---------------------------------------------------------------------------
const API_ENDPOINTS = [
  {
    method: 'POST', path: '/v1/chat',
    desc: 'Customer-facing primary chat proxy. Shadows to candidate.',
    body: {
      messages: [
        { role: 'system', content: 'Reply ONLY as JSON: {"action": "<verb>"}. No prose.' },
        { role: 'user',   content: 'cancel my subscription' },
      ],
      temperature: 0.0,
      max_completion_tokens: 1024,
    },
  },
  {
    method: 'GET',  path: '/v1/metrics',
    desc: 'Real-time business counters: total requests, shadow errors/timeouts, exact match %.',
  },
  {
    method: 'GET',  path: '/v1/config',
    desc: 'Current runtime config: models, evaluator, dispatcher, env_file, DO key length.',
  },
  {
    method: 'PUT',  path: '/v1/config',
    desc: 'Dynamic runtime updates. Try flipping shadow_sample_rate here.',
    body: { shadow_sample_rate: 0.5 },
  },
  {
    method: 'GET',  path: '/v1/evaluations',
    desc: 'List recent comparison records. Supports ?verdict=&limit=&since=.',
    query: { limit: 10 },
  },
  {
    method: 'GET',  path: '/v1/evaluations/summary',
    desc: 'Verdict distribution + latency percentiles over a rolling window.',
    query: { window_seconds: 86400 },
  },
  {
    method: 'GET',  path: '/v1/evaluations/{id}',
    desc: 'Fetch a single comparison row by request_id.',
    pathParam: { id: 'paste-a-request-id' },
  },
  {
    method: 'GET',  path: '/v1/evaluations/{id}/raw',
    desc: 'Pull the full LLM payloads (primary + candidate) from RawStore/Spaces.',
    pathParam: { id: 'paste-a-request-id' },
  },
  {
    method: 'GET',  path: '/healthz',
    desc: 'Liveness probe. Cheap, no DB touch.',
  },
  {
    method: 'GET',  path: '/readyz',
    desc: 'Readiness probe. Verifies DB connectivity.',
  },
  {
    method: 'GET',  path: '/metrics',
    desc: 'Prometheus-format metrics scrape endpoint. Returns text/plain.',
    responseType: 'text',
  },
];

function makeApiCard(ep) {
  const wrap = document.createElement('div');
  wrap.className = 'api-card';
  const methodCls = `method-${ep.method.toLowerCase()}`;

  const pathInputs = ep.pathParam
    ? Object.entries(ep.pathParam).map(([k, v]) =>
        `<label class="mono">{${k}}<input class="api-path-input mono" data-name="${k}" value="${v}" /></label>`
      ).join('')
    : '';

  const queryInputs = ep.query
    ? Object.entries(ep.query).map(([k, v]) =>
        `<label class="mono">?${k}<input class="api-query-input mono" data-name="${k}" value="${v}" /></label>`
      ).join('')
    : '';

  const bodyPre = ep.body
    ? `<details class="api-body-details">
         <summary>Request body (editable JSON)</summary>
         <textarea class="mono api-body">${JSON.stringify(ep.body, null, 2)}</textarea>
       </details>`
    : '';

  wrap.innerHTML = `
    <div class="api-card-head">
      <span class="method-badge ${methodCls}">${ep.method}</span>
      <span class="api-path mono">${ep.path}</span>
      <button class="btn primary sm api-try">Try</button>
    </div>
    <div class="api-desc">${ep.desc}</div>
    ${pathInputs || queryInputs ? `<div class="api-params">${pathInputs}${queryInputs}</div>` : ''}
    ${bodyPre}
    <div class="api-response" hidden>
      <div class="api-response-head">
        <span class="mono api-status"></span>
        <span class="mono api-latency"></span>
      </div>
      <pre class="mono api-response-body"></pre>
    </div>
  `;
  wrap.querySelector('.api-try').addEventListener('click', () => runEndpoint(ep, wrap));
  return wrap;
}

async function runEndpoint(ep, wrap) {
  let url = ep.path;
  if (ep.pathParam) {
    for (const input of wrap.querySelectorAll('.api-path-input')) {
      url = url.replace(`{${input.dataset.name}}`, encodeURIComponent(input.value.trim()));
    }
  }
  if (ep.query) {
    const params = new URLSearchParams();
    for (const input of wrap.querySelectorAll('.api-query-input')) {
      if (input.value.trim() !== '') params.set(input.dataset.name, input.value.trim());
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }

  const opts = {
    method: ep.method,
    headers: { 'Accept': ep.responseType === 'text' ? '*/*' : 'application/json', ...authHeaders() },
  };
  if (ep.body) {
    const raw = wrap.querySelector('.api-body').value;
    try {
      const parsed = JSON.parse(raw);
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(parsed);
    } catch (e) {
      showResponse(wrap, 0, 0, `JSON parse error: ${e.message}`);
      return;
    }
  }

  const respBox = wrap.querySelector('.api-response');
  respBox.hidden = false;
  wrap.querySelector('.api-status').textContent = 'sending…';
  wrap.querySelector('.api-latency').textContent = '';
  wrap.querySelector('.api-response-body').textContent = '';

  const t0 = performance.now();
  try {
    const r = await fetch(url, opts);
    const dt = Math.round(performance.now() - t0);
    let body;
    if (ep.responseType === 'text') {
      body = await r.text();
    } else {
      const text = await r.text();
      try { body = JSON.stringify(JSON.parse(text), null, 2); }
      catch { body = text; }
    }
    showResponse(wrap, r.status, dt, body);
  } catch (e) {
    showResponse(wrap, 0, Math.round(performance.now() - t0), `Network error: ${e.message}`);
  }
}

function showResponse(wrap, status, dt, body) {
  const statusEl  = wrap.querySelector('.api-status');
  const latencyEl = wrap.querySelector('.api-latency');
  const bodyEl    = wrap.querySelector('.api-response-body');
  statusEl.textContent = status ? `HTTP ${status}` : 'ERROR';
  statusEl.className = 'mono api-status ' +
    (status >= 200 && status < 300 ? 'text-ok' : status ? 'text-err' : 'text-err');
  latencyEl.textContent = `${dt}ms`;
  bodyEl.textContent = body || '(empty)';
}

function renderApiExplorer() {
  const host = document.getElementById('api-cards');
  if (!host) return;
  host.innerHTML = '';
  for (const ep of API_ENDPOINTS) host.appendChild(makeApiCard(ep));
}

// Initial paint
renderMetricsSkeleton();
refreshLiveMetrics();
initSampleRateControl();
renderApiExplorer();
refreshStats();
refreshRecent();
setInterval(refreshLiveMetrics, 3_000);
setInterval(refreshStats, 15_000);
setInterval(refreshRecent, 15_000);
