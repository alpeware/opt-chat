let selectedLine = null;
let currentAssistantBubble = null;
let currentStreamedText = '';
const pendingAssistantBubbles = [];
const inFlightRequests = new Map();
let currentWorkspace = '';
let selectedCategory = 'all';
let availableWorkspaces = [];

function getWorkspaceFromHash() {
  if (!window.location.hash) return 'all';
  const clean = decodeURIComponent(window.location.hash.replace(/^#/, '')).trim().toLowerCase();
  return (clean && clean !== 'all') ? clean : 'all';
}

let selectedWorkspace = getWorkspaceFromHash();
let selectedModel = localStorage.getItem('optchat_model') || 'gemini-3.8-flash-high';

function setAppHeight() {
  const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.documentElement.style.setProperty('--app-height', `${h}px`);
  const app = document.getElementById('app');
  if (app) app.style.height = `${h}px`;
}

// Lifecycle and Event Listeners
window.addEventListener('DOMContentLoaded', () => {
  setAppHeight();
  fetchModels();
  fetchState();
  fetchTelemetrySummary();
  fetchHistory();
  connectEventStream();
  setInterval(fetchState, 5000);
  setInterval(fetchTelemetrySummary, 15000);

  window.addEventListener('hashchange', () => {
    const ws = getWorkspaceFromHash();
    if (ws !== selectedWorkspace) {
      setWorkspaceFilter(ws, false);
    }
  });

  const input = document.getElementById('prompt-input');
  if (input) {
    input.addEventListener('focus', () => {
      setTimeout(() => {
        setAppHeight();
        input.scrollIntoView({ block: 'nearest' });
        scrollToBottom();
      }, 300);
    });
    input.addEventListener('blur', () => {
      setTimeout(() => {
        setAppHeight();
        scrollToBottom();
      }, 200);
    });
  }
});

if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', () => {
    setAppHeight();
    scrollToBottom();
  });
  window.visualViewport.addEventListener('scroll', () => {
    if (window.visualViewport.offsetTop > 0) {
      window.scrollTo(0, 0);
    }
  });
}
window.addEventListener('resize', setAppHeight);
window.addEventListener('orientationchange', () => setTimeout(setAppHeight, 200));

// Model Management
async function fetchModels() {
  try {
    const res = await fetch('/api/models');
    if (!res.ok) return;
    const data = await res.json();
    const sel = document.getElementById('model-select-dropdown');
    if (!sel) return;
    if (Array.isArray(data.models) && data.models.length > 0) {
      sel.innerHTML = data.models.map(m =>
        `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name || m.id)}</option>`
      ).join('');
    }
    const saved = localStorage.getItem('optchat_model');
    if (saved) {
      selectedModel = saved;
    } else if (data.current_model) {
      selectedModel = data.current_model;
    }
    sel.value = selectedModel;
  } catch (e) {
    console.warn('Failed to fetch models:', e);
  }
}

async function setModel(model) {
  selectedModel = model || 'gemini-3.8-flash-high';
  localStorage.setItem('optchat_model', selectedModel);
  const sel = document.getElementById('model-select-dropdown');
  if (sel && sel.value !== selectedModel) sel.value = selectedModel;
  try {
    await fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: selectedModel })
    });
  } catch (e) {}
}

// Persistent SSE Event Stream
function connectEventStream() {
  const evtSource = new EventSource('/api/stream');

  evtSource.onmessage = function (e) {
    try {
      const event = JSON.parse(e.data);
      handleStreamEvent(event);
    } catch (err) {}
  };

  evtSource.onerror = function () {
    evtSource.close();
    setTimeout(connectEventStream, 2000);
  };
}

function normalizeMessageText(t) {
  if (!t) return '';
  return t.replace(/\r\n/g, '\n').replace(/\s+/g, ' ').trim();
}

function isMatchingMessageText(a, b) {
  const normA = normalizeMessageText(a);
  const normB = normalizeMessageText(b);
  if (!normA || !normB) return false;
  if (normA === normB) return true;
  if (normA.includes(normB) || normB.includes(normA)) return true;
  const minLen = Math.min(normA.length, normB.length);
  if (minLen >= 20) {
    const prefixLen = Math.min(minLen, 80);
    if (normA.slice(0, prefixLen) === normB.slice(0, prefixLen)) return true;
  }
  return false;
}

function findInFlightRequest(event) {
  const reqId = (event.extra && event.extra.req_id) || event.req_id;
  if (reqId && inFlightRequests.has(reqId)) {
    return inFlightRequests.get(reqId);
  }
  const ws = (event.extra && event.extra.workspace) || event.workspace;
  if (ws) {
    for (const req of inFlightRequests.values()) {
      if (req.workspace.toLowerCase() === ws.toLowerCase()) {
        return req;
      }
    }
  }
  if (inFlightRequests.size === 1) {
    return inFlightRequests.values().next().value;
  }
  return null;
}

function handleStreamEvent(event) {
  if (event.type === 'token' || event.type === 'text') {
    const req = findInFlightRequest(event);
    if (req) {
      req.streamedText += event.content;
      if (req.bubble && document.body.contains(req.bubble)) {
        req.bubble.innerHTML = formatMarkdown(req.streamedText) + '<span class="cursor-stream"></span>';
        const row = req.bubble.closest('.message-row');
        if (row) row.setAttribute('data-raw-text', req.streamedText.trim());
        scrollToBottom();
      }
      return;
    }
    if (!currentAssistantBubble) {
      currentAssistantBubble = pendingAssistantBubbles.shift() || appendMessage('talk', '', new Date().toISOString(), true);
      currentStreamedText = '';
    }
    currentStreamedText += event.content;
    currentAssistantBubble.innerHTML = formatMarkdown(currentStreamedText) + '<span class="cursor-stream"></span>';
    const row = currentAssistantBubble.closest('.message-row');
    if (row) {
      row.setAttribute('data-raw-text', currentStreamedText.trim());
    }
    scrollToBottom();
  } else if (event.type === 'log_talk') {
    const talkText = (event.content || '').trim();
    const req = findInFlightRequest(event);
    if (req) {
      const finalText = talkText || req.streamedText;
      if (req.bubble && document.body.contains(req.bubble)) {
        req.bubble.innerHTML = renderBubbleContent('talk', finalText);
        const row = req.bubble.closest('.message-row');
        if (row) {
          row.setAttribute('data-raw-text', finalText.trim());
          enrichMessageRow(row, 'talk', req.workspace, null, new Date().toISOString());
        }
      }
      inFlightRequests.delete(req.reqId);
      scrollToBottom();
      fetchState();
      return;
    }
    // Check if an existing row in chat-stream already matches this text
    const stream = document.getElementById('chat-stream');
    if (stream) {
      const rows = stream.querySelectorAll('.message-row.talk');
      for (let idx = rows.length - 1; idx >= Math.max(0, rows.length - 8); idx--) {
        const r = rows[idx];
        const rText = r.getAttribute('data-raw-text');
        if (isMatchingMessageText(rText, talkText)) {
          r.setAttribute('data-raw-text', talkText);
          currentAssistantBubble = null;
          currentStreamedText = '';
          scrollToBottom();
          fetchState();
          return;
        }
      }
    }
    if (!currentAssistantBubble) {
      currentAssistantBubble = pendingAssistantBubbles.shift() || appendMessage('talk', '', new Date().toISOString(), false);
    }
    currentStreamedText = event.content;
    currentAssistantBubble.innerHTML = renderBubbleContent('talk', currentStreamedText);
    const row = currentAssistantBubble.closest('.message-row');
    if (row) {
      row.setAttribute('data-raw-text', currentStreamedText.trim());
      let timeEl = row.querySelector('.message-time');
      if (!timeEl) {
        const header = row.querySelector('.message-header');
        if (header) {
          timeEl = document.createElement('span');
          timeEl.className = 'message-time';
          header.appendChild(timeEl);
        }
      }
      if (timeEl && !timeEl.textContent) {
        const nowIso = new Date().toISOString();
        timeEl.textContent = formatTimestamp(nowIso);
        timeEl.title = nowIso;
      }
    }
    currentAssistantBubble = null;
    currentStreamedText = '';
    scrollToBottom();
    fetchState();
  } else if (event.type === 'settling' || event.type === 'settling_timeout') {
    const req = findInFlightRequest(event);
    if (req) {
      req.status = event.content;
      if (req.bubble && document.body.contains(req.bubble) && !req.streamedText) {
        req.bubble.innerHTML = `<span style="color: var(--text-dim); font-style: italic;">⏳ ${escapeHtml(event.content)}</span>`;
      }
      return;
    }
    const target = currentAssistantBubble || (pendingAssistantBubbles.length > 0 ? pendingAssistantBubbles[0] : null);
    if (target && !currentStreamedText) {
      target.innerHTML = `<span style="color: var(--text-dim); font-style: italic;">⏳ ${escapeHtml(event.content)}</span>`;
    }
  } else if (event.type === 'tool_call' || event.type === 'log_tool') {
    const toolEl = document.createElement('details');
    toolEl.className = 'tool-box';
    toolEl.innerHTML = `<summary>⚡ Tool: ${escapeHtml(event.content)}</summary><pre>${escapeHtml(event.content)}</pre>`;
    const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
    document.getElementById('chat-stream').insertBefore(toolEl, target);
    scrollToBottom();
  } else if (event.type === 'log_echo') {
    const echoEl = document.createElement('details');
    echoEl.className = 'tool-box';
    echoEl.innerHTML = `<summary>📄 Tool Result</summary><pre>${escapeHtml(event.content)}</pre>`;
    const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
    document.getElementById('chat-stream').insertBefore(echoEl, target);
    scrollToBottom();
  } else if (event.type === 'subagent_spawn') {
    const role = (event.extra && event.extra.role) || 'Subagent';
    const typeName = (event.extra && event.extra.typeName) || 'subagent';
    const prompt = (event.extra && event.extra.prompt) || event.content;
    const card = document.createElement('div');
    card.className = 'subagent-card';
    card.innerHTML = `
      <div class="subagent-header">
        <span>🚀 Subagent: <b>${escapeHtml(role)}</b></span>
        <span class="subagent-badge">${escapeHtml(typeName)}</span>
      </div>
      <div class="subagent-prompt">${formatMarkdown(prompt)}</div>
      <div class="subagent-tools"></div>
    `;
    const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
    document.getElementById('chat-stream').insertBefore(card, target);
    scrollToBottom();
  } else if (event.type === 'subagent_tool') {
    const toolName = (event.extra && event.extra.tool) || 'tool';
    const toolArgs = (event.extra && event.extra.args) || {};
    const toolEl = document.createElement('details');
    toolEl.className = 'tool-box';
    toolEl.style.borderLeftColor = 'var(--purple)';
    const argsPreview = JSON.stringify(toolArgs);
    toolEl.innerHTML = `<summary>⚡ Subagent Tool: <b>${escapeHtml(toolName)}</b> <span style="opacity: 0.7; font-weight: normal; margin-left: 6px;">${escapeHtml(argsPreview.slice(0, 90))}</span></summary><pre>${escapeHtml(JSON.stringify(toolArgs, null, 2))}</pre>`;
    const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
    document.getElementById('chat-stream').insertBefore(toolEl, target);
    scrollToBottom();
  } else if (event.type === 'subagent_complete') {
    scrollToBottom();
  } else if (event.type === 'subagent_report') {
    const repText = (event.extra && event.extra.report) || event.content;
    const role = (event.extra && event.extra.role) || 'Subagent';
    appendMessage('work', `### 📋 Subagent Report: ${role}\n\n${repText}`, new Date().toISOString(), false);
    scrollToBottom();
    fetchState();
  } else if (event.type === 'error') {
    const req = findInFlightRequest(event);
    if (req) {
      if (req.bubble && document.body.contains(req.bubble)) {
        req.bubble.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(event.content)}</span>`;
      }
      inFlightRequests.delete(req.reqId);
      return;
    }
    if (currentAssistantBubble) {
      currentAssistantBubble.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(event.content)}</span>`;
      currentAssistantBubble = null;
    } else if (pendingAssistantBubbles.length > 0) {
      const pb = pendingAssistantBubbles.shift();
      pb.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(event.content)}</span>`;
    }
  } else if (event.type === 'new_message') {
    const kind = (event.extra && event.extra.kind) || 'talk';
    const text = (event.content || '').trim();
    const date = (event.extra && event.extra.date) || new Date().toISOString();
    const ws = (event.extra && event.extra.workspace);
    const dev = (event.extra && event.extra.device);
    const msgId = (event.extra && event.extra.i);
    const wsMatch = (!selectedWorkspace || selectedWorkspace === 'all' || (ws && ws.toLowerCase() === selectedWorkspace.toLowerCase()));
    let catMatch = true;
    if (selectedCategory && selectedCategory !== 'all') {
      if (selectedCategory === 'main') catMatch = (kind === 'user' || kind === 'talk');
      else if (selectedCategory === 'subagent') catMatch = (kind === 'work');
      else if (selectedCategory === 'note') catMatch = (kind === 'note');
    }

    // Deduplication check: reconcile against existing optimistic or streamed bubbles
    const stream = document.getElementById('chat-stream');
    let matchedRow = null;
    if (stream) {
      const rows = stream.querySelectorAll('.message-row');
      // 1. If this exact message ID is already rendered, ignore completely
      if (msgId !== undefined && msgId !== null) {
        for (let idx = rows.length - 1; idx >= Math.max(0, rows.length - 20); idx--) {
          if (rows[idx].getAttribute('data-msg-id') === String(msgId)) {
            fetchState();
            return;
          }
        }
      }

      // 2. Check if active assistant bubble is currently streaming or pending this reply
      if (kind === 'talk' && (currentAssistantBubble || pendingAssistantBubbles.length > 0)) {
        const targetBubble = currentAssistantBubble || pendingAssistantBubbles.shift();
        if (targetBubble) {
          targetBubble.innerHTML = renderBubbleContent('talk', text);
          const r = targetBubble.closest('.message-row');
          if (r) {
            r.setAttribute('data-raw-text', text);
            if (msgId !== undefined && msgId !== null) r.setAttribute('data-msg-id', String(msgId));
            enrichMessageRow(r, kind, ws, dev, date);
          }
          currentAssistantBubble = null;
          currentStreamedText = '';
          scrollToBottom();
          fetchState();
          return;
        }
      }

      // 3. Check for any recent row of matching kind with matching text (reconcile optimistic/streamed)
      for (let idx = rows.length - 1; idx >= Math.max(0, rows.length - 12); idx--) {
        const r = rows[idx];
        const rKind = r.classList.contains(kind) || (!r.classList.contains('user') && kind === 'talk');
        if (rKind) {
          const rText = r.getAttribute('data-raw-text');
          if (isMatchingMessageText(rText, text)) {
            matchedRow = r;
            break;
          }
        }
      }
    }

    if (matchedRow) {
      if (msgId !== undefined && msgId !== null) matchedRow.setAttribute('data-msg-id', String(msgId));
      enrichMessageRow(matchedRow, kind, ws, dev, date);
      scrollToBottom();
      fetchState();
      return;
    }

    if (wsMatch && catMatch) {
      const newBubble = appendMessage(kind, text, date, false, null, ws, dev);
      if (newBubble) {
        const r = newBubble.closest('.message-row');
        if (r) {
          r.setAttribute('data-raw-text', text);
          if (msgId !== undefined && msgId !== null) r.setAttribute('data-msg-id', String(msgId));
        }
      }
      scrollToBottom();
    }
    fetchState();
  } else if (event.type === 'turn_complete') {
    const tokens = event.extra ? event.extra.tokens : null;
    const req = findInFlightRequest(event);
    if (req) {
      if (req.bubble && document.body.contains(req.bubble) && req.streamedText) {
        req.bubble.innerHTML = renderBubbleContent('talk', req.streamedText);
        const row = req.bubble.closest('.message-row');
        if (row) {
          row.setAttribute('data-raw-text', req.streamedText.trim());
          if (tokens) attachTokenBadgeToRow(row, tokens);
        }
      }
      inFlightRequests.delete(req.reqId);
      fetchState();
      return;
    }
    if (currentAssistantBubble) {
      if (currentStreamedText) {
        currentAssistantBubble.innerHTML = renderBubbleContent('talk', currentStreamedText);
        const row = currentAssistantBubble.closest('.message-row');
        if (row) {
          row.setAttribute('data-raw-text', currentStreamedText.trim());
          if (tokens) attachTokenBadgeToRow(row, tokens);
        }
      }
      currentAssistantBubble = null;
      currentStreamedText = '';
    }
    fetchState();
  }
}

function setCategoryFilter(cat) {
  selectedCategory = cat || 'all';
  document.querySelectorAll('#category-pills .filter-btn').forEach(btn => {
    const bcat = btn.getAttribute('data-category') || '';
    btn.classList.toggle('active', bcat === selectedCategory);
  });
  fetchHistory();
}

function renderWorkspacePills(workspaces) {
  const container = document.getElementById('workspace-pills');
  if (!container) return;
  const wsList = Array.isArray(workspaces) ? workspaces : [];

  let html = `<button type="button" class="filter-btn ${selectedWorkspace === 'all' ? 'active' : ''}" data-workspace="all" onclick="setWorkspaceFilter('all')">🏷️ All</button>`;
  for (const w of wsList) {
    if (!w) continue;
    const isAct = (selectedWorkspace.toLowerCase() === w.toLowerCase());
    html += `<button type="button" class="filter-btn ${isAct ? 'active' : ''}" data-workspace="${escapeHtml(w)}" onclick="setWorkspaceFilter('${escapeHtml(w)}')">📁 ${escapeHtml(w)}</button>`;
  }
  container.innerHTML = html;

  const sel = document.getElementById('ws-select-dropdown');
  if (sel) {
    let opts = '<option value="all">All Workspaces</option>';
    for (const w of wsList) {
      if (!w) continue;
      opts += `<option value="${escapeHtml(w)}">${escapeHtml(w)}</option>`;
    }
    sel.innerHTML = opts;
    sel.value = selectedWorkspace;
  }
}

function setWorkspaceFilter(ws, updateHash = true) {
  selectedWorkspace = (ws || 'all').toLowerCase();

  if (updateHash) {
    if (selectedWorkspace === 'all') {
      if (window.location.hash) {
        history.replaceState(null, '', window.location.pathname + window.location.search);
      }
    } else {
      const targetHash = '#' + encodeURIComponent(selectedWorkspace);
      if (window.location.hash !== targetHash) {
        history.replaceState(null, '', targetHash);
      }
    }
  }

  // Update workspace pills highlight
  document.querySelectorAll('#workspace-pills .filter-btn').forEach(btn => {
    const bws = (btn.getAttribute('data-workspace') || '').toLowerCase();
    btn.classList.toggle('active', bws === selectedWorkspace);
  });

  // Sync select dropdown
  const sel = document.getElementById('ws-select-dropdown');
  if (sel) {
    let found = false;
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value.toLowerCase() === selectedWorkspace) {
        sel.selectedIndex = i;
        found = true;
        break;
      }
    }
    if (!found && selectedWorkspace === 'all') sel.value = 'all';
  }

  // Update header workspace pill
  const wsEl = document.getElementById('pill-ws');
  if (wsEl) {
    wsEl.textContent = selectedWorkspace === 'all' ? '📁 All' : '📁 ' + selectedWorkspace;
  }

  fetchHistory();
}

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    const data = await res.json();
    const pillMsgs = document.getElementById('pill-msgs');
    if (pillMsgs && data.messages_count !== undefined) {
      pillMsgs.textContent = `Msgs: ${data.messages_count.toLocaleString()}`;
    }
    const pillNodes = document.getElementById('pill-nodes');
    if (pillNodes && data.tree_nodes_count !== undefined) {
      pillNodes.textContent = `Nodes: ${data.tree_nodes_count.toLocaleString()}`;
    }
    if (data.view_budget) {
      const pct = (((data.view_size || 0) / data.view_budget) * 100).toFixed(0);
      const pillView = document.getElementById('pill-view');
      if (pillView) pillView.textContent = `View: ${pct}%`;
    }
    const statusDot = document.getElementById('status-dot');
    if (statusDot) {
      statusDot.style.background = data.is_settled ? '#10b981' : '#f59e0b';
    }
    if (data.workspaces && Array.isArray(data.workspaces)) {
      availableWorkspaces = data.workspaces;
      renderWorkspacePills(availableWorkspaces);
    }
    if (data.workspace) {
      currentWorkspace = data.workspace;
      const parts = data.workspace.split('/').filter(Boolean);
      const name = parts.length > 0 ? parts[parts.length - 1] : '/';
      const wsEl = document.getElementById('pill-ws');
      if (wsEl) {
        const filterLabel = selectedWorkspace && selectedWorkspace !== 'all' ? selectedWorkspace : name;
        wsEl.textContent = '📁 ' + filterLabel;
      }
    }
  } catch (e) {
    const statusDot = document.getElementById('status-dot');
    if (statusDot) statusDot.style.background = '#ef4444';
  }
}

async function openSessionModal() {
  let wsListStr = availableWorkspaces.length > 0 ? "\nDiscovered Workspaces: " + availableWorkspaces.join(", ") : "";
  const choice = prompt(
    `Filter by Workspace or Switch Directory:\n\nActive Directory: ${currentWorkspace}\nCurrent Filter: ${selectedWorkspace}${wsListStr}\n\nType a workspace name to filter (or 'all' for all messages), or enter an absolute path starting with '/' to change active directory:`,
    selectedWorkspace
  );
  if (!choice) return;
  const trimmed = choice.trim();
  if (trimmed.startsWith('/')) {
    try {
      const res = await fetch('/api/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workspace: trimmed })
      });
      const data = await res.json();
      if (data.error) {
        alert('Failed to switch workspace: ' + data.error);
      } else {
        fetchState();
      }
    } catch (e) {
      alert('Error: ' + e.message);
    }
  } else {
    selectedWorkspace = trimmed.toLowerCase();
    const wsEl = document.getElementById('pill-ws');
    if (wsEl) {
      wsEl.textContent = selectedWorkspace === 'all' ? '📁 All' : '📁 ' + selectedWorkspace;
    }
    fetchHistory();
    fetchState();
  }
}

let isFoldedView = false;
const openBlocks = new Map();

async function fetchHistory() {
  if (isFoldedView) {
    await renderFoldedView();
    return;
  }
  try {
    const wsQuery = selectedWorkspace && selectedWorkspace !== 'all' ? `&workspace=${encodeURIComponent(selectedWorkspace)}` : '';
    const catQuery = selectedCategory && selectedCategory !== 'all' ? `&category=${encodeURIComponent(selectedCategory)}` : '';
    const res = await fetch(`/api/history?limit=50${wsQuery}${catQuery}`);
    if (!res.ok) {
      console.error('Failed to load history:', res.status, res.statusText);
      return;
    }
    const msgs = await res.json();
    const stream = document.getElementById('chat-stream');
    if (Array.isArray(msgs)) {
      stream.innerHTML = '';
      let matchingInFlight = false;
      inFlightRequests.forEach(req => {
        if (!selectedWorkspace || selectedWorkspace === 'all' || req.workspace === 'all' || req.workspace.toLowerCase() === selectedWorkspace.toLowerCase()) {
          matchingInFlight = true;
        }
      });

      if (msgs.length === 0 && !matchingInFlight) {
        stream.innerHTML = `<div class="empty-stream-notice" style="text-align: center; padding: 40px 20px; color: var(--text-dim); font-size: 0.9rem;">No messages in workspace <b>${escapeHtml(selectedWorkspace)}</b>.</div>`;
        return;
      }

      // Check if earlier messages exist in tree
      try {
        const viewRes = await fetch('/api/view');
        if (viewRes.ok) {
          const viewData = await viewRes.json();
          if (viewData.lines && viewData.lines.some(l => !l.startsWith('0+1|') && !l.includes('+1|'))) {
            const banner = document.createElement('div');
            banner.className = 'tree-folded-banner';
            banner.innerHTML = `<span>🗂️ <b>Memory Tree:</b> Earlier chat is folded in summary blocks.</span><span class="banner-cta">Fold all ▾</span>`;
            banner.onclick = () => toggleFoldedView();
            stream.appendChild(banner);
          }
        }
      } catch (_) {}

      msgs.forEach((m) => {
        const b = appendMessage(m.kind, m.text, m.date, false, null, m.workspace, m.device);
        if (b && m.i !== undefined) {
          const r = b.closest('.message-row');
          if (r) {
            r.setAttribute('data-msg-id', String(m.i));
            r.setAttribute('data-raw-text', (m.text || '').trim());
          }
        }
      });

      // Re-attach active in-flight requests that match the current workspace filter
      inFlightRequests.forEach(req => {
        const wsMatch = (!selectedWorkspace || selectedWorkspace === 'all' || req.workspace === 'all' || req.workspace.toLowerCase() === selectedWorkspace.toLowerCase());
        if (wsMatch) {
          const uBubble = appendMessage('user', req.userText, req.date, false, null, req.workspace !== 'all' ? req.workspace : null);
          if (uBubble) {
            const uRow = uBubble.closest('.message-row');
            if (uRow) {
              uRow.setAttribute('data-raw-text', req.userText.trim());
              uRow.setAttribute('data-req-id', req.reqId);
              uRow.setAttribute('data-workspace', req.workspace);
            }
          }
          const aBubble = appendMessage('talk', '', req.date, true, null, req.workspace !== 'all' ? req.workspace : null);
          if (aBubble) {
            const aRow = aBubble.closest('.message-row');
            if (aRow) {
              aRow.setAttribute('data-req-id', req.reqId);
              aRow.setAttribute('data-workspace', req.workspace);
            }
            req.bubble = aBubble;
            if (req.streamedText) {
              aBubble.innerHTML = formatMarkdown(req.streamedText) + '<span class="cursor-stream"></span>';
              aRow.setAttribute('data-raw-text', req.streamedText.trim());
            } else {
              aBubble.innerHTML = `<span style="color: var(--text-dim); font-style: italic;">⏳ ${escapeHtml(req.status || 'Running turn...')}</span>`;
            }
          }
        } else {
          req.bubble = null;
        }
      });

      scrollToBottom();
    }
  } catch (e) {
    console.error('Error fetching history:', e);
  }
}

async function toggleFoldedView() {
  isFoldedView = !isFoldedView;
  const foldLabel = document.getElementById('btn-fold-label');
  const foldPill = document.getElementById('pill-fold-all');
  if (foldLabel) foldLabel.textContent = isFoldedView ? 'Stream' : 'Folded';
  if (foldPill) foldPill.textContent = isFoldedView ? '💬 Stream' : '🗂️ Fold all';

  if (isFoldedView) {
    await renderFoldedView();
  } else {
    await fetchHistory();
  }
}

async function renderFoldedView() {
  const stream = document.getElementById('chat-stream');
  if (!stream) return;
  stream.innerHTML = `
    <div class="folded-view-header">
      <span>🗂️ <b>Memory Tree (Folded View)</b></span>
      <span class="fold-hint">Click any block to unfold sub-summaries</span>
    </div>
    <div id="tree-blocks-container" style="display: flex; flex-direction: column; gap: 8px; width: 100%;">
      <p style="color: var(--text-dim); text-align: center; padding: 20px;">Loading tree blocks...</p>
    </div>
  `;

  try {
    const wsQuery = selectedWorkspace && selectedWorkspace !== 'all' ? `?workspace=${encodeURIComponent(selectedWorkspace)}` : '';
    const res = await fetch(`/api/view${wsQuery}`);
    if (!res.ok) throw new Error('Failed to load view');
    const data = await res.json();
    const container = document.getElementById('tree-blocks-container');
    container.innerHTML = '';
    const lines = data.lines || [];
    if (!lines.length) {
      container.innerHTML = '<p style="color: var(--text-dim); text-align: center;">Tree is empty.</p>';
      return;
    }
    for (const line of lines) {
      const pipeIdx = line.indexOf('|');
      if (pipeIdx === -1) continue;
      const coord = line.slice(0, pipeIdx);
      const text = line.slice(pipeIdx + 1);
      const parts = coord.split('+');
      const id = parseInt(parts[0], 10);
      const n = parseInt(parts[1], 10);
      container.appendChild(createBlockElement(id, n, text));
    }
  } catch (e) {
    const container = document.getElementById('tree-blocks-container');
    if (container) {
      container.innerHTML = `<p style="color: #ef4444; text-align: center;">Error loading tree: ${escapeHtml(e.message)}</p>`;
    }
  }
}

function createBlockElement(id, n, text) {
  const key = `${id}+${n}`;
  if (openBlocks.has(key)) {
    return createOpenBoxElement(id, n, text, openBlocks.get(key));
  }

  const block = document.createElement('div');
  block.className = 'tree-block';
  block.id = `block-${id}-${n}`;
  block.onclick = (e) => {
    e.stopPropagation();
    unfoldBlock(id, n, text);
  };
  block.innerHTML = `
    <span class="block-badge">${n}x</span>
    <div class="block-text" title="${escapeHtml(text)}">${escapeHtml(text)}</div>
    <button class="block-unfold-btn" type="button">Unfold ▾</button>
  `;
  return block;
}

function createOpenBoxElement(id, n, text, zoomData) {
  const box = document.createElement('div');
  box.className = 'tree-box';
  box.id = `box-${id}-${n}`;

  const header = document.createElement('div');
  header.className = 'tree-box-header';
  const rangeLabel = n > 1 ? `[#${id}–#${id + n - 1}]` : `[#${id}]`;
  header.innerHTML = `<span>▲ Fold <b>${n}x</b> ${rangeLabel}</span>`;
  header.onclick = (e) => {
    e.stopPropagation();
    foldBlock(id, n, text);
  };
  box.appendChild(header);

  if (zoomData.is_leaf && zoomData.message) {
    const bubbleWrapper = document.createElement('div');
    bubbleWrapper.className = 'tree-box-body';
    const m = zoomData.message;
    const msgRow = document.createElement('div');
    msgRow.className = `message-row ${m.kind}`;
    
    const msgHeader = document.createElement('div');
    msgHeader.className = 'message-header';
    const sender = document.createElement('span');
    sender.className = 'message-sender';
    sender.textContent = getSenderLabel(m.kind, m.text);
    msgHeader.appendChild(sender);
    if (m.date) {
      const timeEl = document.createElement('span');
      timeEl.className = 'message-time';
      timeEl.textContent = formatTimestamp(m.date);
      msgHeader.appendChild(timeEl);
    }
    
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = renderBubbleContent(m.kind, m.text);
    
    msgRow.appendChild(msgHeader);
    msgRow.appendChild(bubble);
    bubbleWrapper.appendChild(msgRow);
    box.appendChild(bubbleWrapper);
  } else if (Array.isArray(zoomData.children)) {
    const childrenContainer = document.createElement('div');
    childrenContainer.className = 'tree-box-children';
    for (const child of zoomData.children) {
      childrenContainer.appendChild(createBlockElement(child.id, child.n, child.text));
    }
    box.appendChild(childrenContainer);
  }
  return box;
}

async function unfoldBlock(id, n, text) {
  const key = `${id}+${n}`;
  const el = document.getElementById(`block-${id}-${n}`);
  if (el) {
    const btn = el.querySelector('.block-unfold-btn');
    if (btn) btn.textContent = 'Opening...';
  }

  try {
    const res = await fetch('/api/zoom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, n })
    });
    const data = await res.json();
    openBlocks.set(key, data);

    const newEl = createBlockElement(id, n, text);
    if (el && el.parentNode) {
      el.parentNode.replaceChild(newEl, el);
    }
  } catch (e) {
    console.error('Failed to unfold block:', e);
    if (el) {
      const btn = el.querySelector('.block-unfold-btn');
      if (btn) btn.textContent = 'Error';
    }
  }
}

function foldBlock(id, n, text) {
  const key = `${id}+${n}`;
  openBlocks.delete(key);
  const el = document.getElementById(`box-${id}-${n}`);
  const newEl = createBlockElement(id, n, text);
  if (el && el.parentNode) {
    el.parentNode.replaceChild(newEl, el);
  }
}

function scrollToBottom() {
  const stream = document.getElementById('chat-stream');
  if (stream) {
    stream.scrollTop = stream.scrollHeight;
  }
}

function autoGrow(textarea) {
  textarea.style.height = 'auto';
  const scrollH = textarea.scrollHeight;
  const targetH = Math.min(Math.max(scrollH, 46), 160);
  textarea.style.height = targetH + 'px';
}

function handleKeyDown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    sendMessage();
  }
}

function quickSend(cmd) {
  if (cmd === '/tokens' || cmd === '/telemetry') {
    openTelemetryDrawer();
    return;
  }
  document.getElementById('prompt-input').value = cmd;
  sendMessage();
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatTimestamp(dateStr) {
  if (!dateStr) return '';
  try {
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    const timeStr = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (isToday) {
      return timeStr;
    }
    const monthStr = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return `${monthStr}, ${timeStr}`;
  } catch (e) {
    return '';
  }
}

function getSenderLabel(kind, text) {
  if (kind === 'user') return 'You';
  if (kind === 'talk') return 'OptChat';
  if (kind === 'work') {
    const match = text && text.match(/Subagent Report:?\s*([^\n\r]+)/i);
    if (match) return `Worker [${match[1].trim()}]`;
    return 'Worker';
  }
  if (kind === 'note') return 'Note';
  return kind.toUpperCase();
}

function getMessagePreview(text, kind) {
  if (!text) return kind === 'work' ? 'Subagent report' : 'Message';
  let clean = text
    .replace(/```[\s\S]*?```/g, ' [code] ')
    .replace(/\$\$[\s\S]*?\$\$/g, ' [formula] ')
    .replace(/#/g, '')
    .replace(/[*_`~>|]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!clean) return kind === 'work' ? 'Subagent report' : 'Message';
  if (clean.length > 130) {
    return clean.slice(0, 130) + '...';
  }
  return clean;
}

function renderBubbleContent(kind, text, threadId = null) {
  const isMultiLine = text.includes('\n') || text.length > 100;
  const isCollapsible = kind === 'work' || isMultiLine;
  if (!isCollapsible) {
    return formatMarkdown(text);
  }
  const isOpen = kind !== 'work';
  const preview = escapeHtml(getMessagePreview(text, kind));
  let fullHtml = formatMarkdown(text);

  return `
    <details class="msg-details" ${isOpen ? 'open' : ''}>
      <summary class="msg-summary">
        <span class="summary-icon"></span>
        <span class="summary-preview">${preview}</span>
        <span class="summary-action"></span>
      </summary>
      <div class="msg-body">${fullHtml}</div>
    </details>
  `;
}

function enrichMessageRow(row, kind, ws, dev, date) {
  let header = row.querySelector('.message-header');
  if (!header) return;

  if (ws && !header.querySelector('.message-workspace-badge')) {
    const wsBadge = document.createElement('span');
    wsBadge.className = 'message-workspace-badge';
    wsBadge.textContent = '🏷️ ' + ws;
    wsBadge.title = 'Click to filter by workspace: ' + ws;
    wsBadge.onclick = (e) => {
      e.stopPropagation();
      setWorkspaceFilter(ws);
    };
    header.appendChild(wsBadge);
  }

  if (dev && !header.querySelector('.message-device-badge')) {
    const devBadge = document.createElement('span');
    devBadge.className = 'message-device-badge';
    const devLower = dev.toLowerCase();
    const devIcon = (devLower.includes('pixel') || devLower.includes('phone') || devLower.includes('android')) ? '📱 ' : '💻 ';
    devBadge.textContent = devIcon + dev;
    devBadge.title = 'Device: ' + dev;
    header.appendChild(devBadge);
  }

  if (date) {
    let timeEl = header.querySelector('.message-time');
    if (!timeEl) {
      timeEl = document.createElement('span');
      timeEl.className = 'message-time';
      header.appendChild(timeEl);
    }
    timeEl.textContent = formatTimestamp(date);
    timeEl.title = date;
  }
}

function appendMessage(kind, text, dateStr, isStreaming = false, threadId = null, workspace = null, device = null) {
  const stream = document.getElementById('chat-stream');
  if (!stream) return null;

  const row = document.createElement('div');
  row.className = `message-row ${kind}`;
  if (text) {
    row.setAttribute('data-raw-text', text.trim());
  }

  const header = document.createElement('div');
  header.className = 'message-header';

  const sender = document.createElement('span');
  sender.className = 'message-sender';
  sender.textContent = getSenderLabel(kind, text);
  header.appendChild(sender);

  if (workspace) {
    const wsBadge = document.createElement('span');
    wsBadge.className = 'message-workspace-badge';
    wsBadge.textContent = '🏷️ ' + workspace;
    wsBadge.title = 'Click to filter by workspace: ' + workspace;
    wsBadge.onclick = (e) => {
      e.stopPropagation();
      setWorkspaceFilter(workspace);
    };
    header.appendChild(wsBadge);
  }

  if (device) {
    const devBadge = document.createElement('span');
    devBadge.className = 'message-device-badge';
    const devLower = device.toLowerCase();
    const devIcon = (devLower.includes('pixel') || devLower.includes('phone') || devLower.includes('android')) ? '📱 ' : '💻 ';
    devBadge.textContent = devIcon + device;
    devBadge.title = 'Device: ' + device;
    header.appendChild(devBadge);
  }

  if (dateStr) {
    const timeEl = document.createElement('span');
    timeEl.className = 'message-time';
    timeEl.textContent = formatTimestamp(dateStr);
    timeEl.title = dateStr;
    header.appendChild(timeEl);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (isStreaming) {
    bubble.innerHTML = formatMarkdown(text) + '<span class="cursor-stream"></span>';
  } else {
    bubble.innerHTML = renderBubbleContent(kind, text, threadId);
  }

  row.appendChild(header);
  row.appendChild(bubble);
  stream.appendChild(row);
  scrollToBottom();
  return bubble;
}

// Hydrate math when user opens a collapsible message
document.addEventListener('toggle', (e) => {
  if (e.target && e.target.open && e.target.classList.contains('msg-details')) {
    hydratePendingMath();
  }
}, true);

// KaTeX Math Rendering Helpers
function renderMathToken(math, display) {
  if (typeof katex !== 'undefined' && katex.renderToString) {
    try {
      return katex.renderToString(math, {
        displayMode: display,
        throwOnError: false
      });
    } catch (e) {
      const esc = escapeHtml(math);
      return display ? `<div class="katex-display-wrapper"><pre><code>$$${esc}$$</code></pre></div>` : `<code>$${esc}$</code>`;
    }
  }
  const esc = escapeHtml(math);
  return `<span class="math-pending" data-math="${encodeURIComponent(math)}" data-display="${display}">${display ? '$$' + esc + '$$' : '$' + esc + '$'}</span>`;
}

function hydratePendingMath() {
  if (typeof katex === 'undefined') return;
  document.querySelectorAll('.math-pending').forEach(el => {
    try {
      const rawMath = decodeURIComponent(el.getAttribute('data-math') || '');
      const isDisp = el.getAttribute('data-display') === 'true';
      const rendered = katex.renderToString(rawMath, { displayMode: isDisp, throwOnError: false });
      el.outerHTML = rendered;
    } catch (e) {}
  });
}

window.addEventListener('load', hydratePendingMath);
setTimeout(hydratePendingMath, 400);
setTimeout(hydratePendingMath, 1200);
setTimeout(hydratePendingMath, 3000);

// Markdown parser with table, code, and KaTeX math support
function formatMarkdown(text) {
  if (!text) return '';

  const codeBlocks = [];
  const mathBlocks = [];

  // 1. Protect code blocks ```...```
  let processed = text.replace(/```([a-z0-9_-]*)\n([\s\S]*?)```/gi, (match, lang, code) => {
    const idx = codeBlocks.length;
    const escCode = escapeHtml(code.trim());
    codeBlocks.push(`<pre><code>${escCode}</code></pre>`);
    return `@@OPTCHAT_CODE_${idx}@@`;
  });

  // 2. Protect inline code `...`
  processed = processed.replace(/`([^`]+)`/g, (match, code) => {
    const idx = codeBlocks.length;
    const escCode = escapeHtml(code);
    codeBlocks.push(`<code>${escCode}</code>`);
    return `@@OPTCHAT_CODE_${idx}@@`;
  });

  // 3. Extract display math: $$...$$ and \[...\]
  processed = processed.replace(/\$\$([\s\S]*?)\$\$/g, (match, math) => {
    const idx = mathBlocks.length;
    mathBlocks.push({ math: math.trim(), display: true });
    return `\n@@OPTCHAT_MATH_${idx}@@\n`;
  });
  processed = processed.replace(/\\\[([\s\S]*?)\\\]/g, (match, math) => {
    const idx = mathBlocks.length;
    mathBlocks.push({ math: math.trim(), display: true });
    return `\n@@OPTCHAT_MATH_${idx}@@\n`;
  });

  // 4. Extract inline math: $...$ and \(...\)
  processed = processed.replace(/(?<!\\)\$([^\$\s\n](?:[^\$\n]*?[^\$\s\n])?)(?<!\\)\$/g, (match, math) => {
    const idx = mathBlocks.length;
    mathBlocks.push({ math: math.trim(), display: false });
    return `@@OPTCHAT_MATH_${idx}@@`;
  });
  processed = processed.replace(/\\\(([\s\S]*?)\\\)/g, (match, math) => {
    const idx = mathBlocks.length;
    mathBlocks.push({ math: math.trim(), display: false });
    return `@@OPTCHAT_MATH_${idx}@@`;
  });

  // 5. Escape HTML for remaining prose
  let escaped = escapeHtml(processed);

  // Bold & Italic
  escaped = escaped.replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>');
  escaped = escaped.replace(/\*([^\*]+)\*/g, '<em>$1</em>');

  // Markdown file links: [title](file:///path/to/file)
  escaped = escaped.replace(/\[([^\]]+)\]\(file:\/\/([^\)]+)\)/g, (match, title, path) => {
    let cleanPath = path.trim();
    if (!cleanPath.startsWith('/')) cleanPath = '/' + cleanPath;
    const url = `/api/file?path=${encodeURIComponent(cleanPath)}`;
    return `<a href="${url}" target="_blank" class="file-link" style="color: #38bdf8; text-decoration: underline; text-underline-offset: 3px;">📄 ${title}</a>`;
  });

  // Markdown links: [title](url)
  escaped = escaped.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" style="color: var(--accent); text-decoration: underline;">$1</a>');

  // Plain file:/// links
  escaped = escaped.replace(/(^|[^"'>])(file:\/\/\/?[^\s<]+)/g, (match, prefix, fullUrl) => {
    let cleanPath = fullUrl.replace(/^file:\/\//, '').trim();
    cleanPath = cleanPath.replace(/[\.,;:!\)]+$/, '');
    if (!cleanPath.startsWith('/')) cleanPath = '/' + cleanPath;
    const url = `/api/file?path=${encodeURIComponent(cleanPath)}`;
    const filename = cleanPath.split('/').filter(Boolean).pop() || cleanPath;
    return `${prefix}<a href="${url}" target="_blank" class="file-link" style="color: #38bdf8; text-decoration: underline; text-underline-offset: 3px;">📄 ${filename}</a>`;
  });

  // Plain URLs
  escaped = escaped.replace(/(^|[^"'>])(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" style="color: var(--accent);">$2</a>');

  // Tables, headers, lists, blockquotes, paragraphs
  const lines = escaped.split('\n');
  const output = [];
  let i = 0;

  function isTableSep(str) {
    const s = str.trim();
    if (!s.includes('|') || !s.includes('-')) return false;
    return /^[|]?([\s]*:?-+:?[\s]*[|])*[\s]*:?-+:?[\s]*[|]?$/.test(s);
  }

  function parseCells(str) {
    let s = str.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|')) s = s.slice(0, -1);
    return s.split('|').map(c => c.trim());
  }

  function getAlign(str) {
    const s = str.trim();
    const l = s.startsWith(':');
    const r = s.endsWith(':');
    if (l && r) return 'center';
    if (r) return 'right';
    if (l) return 'left';
    return '';
  }

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      output.push('<br>');
      i++;
      continue;
    }

    // Check if line is a standalone display math placeholder
    const dispMathMatch = trimmed.match(/^@@OPTCHAT_MATH_(\d+)@@$/);
    if (dispMathMatch) {
      const idx = parseInt(dispMathMatch[1], 10);
      const b = mathBlocks[idx];
      if (b && b.display) {
        output.push(`<div class="katex-display-wrapper">${renderMathToken(b.math, true)}</div>`);
        i++;
        continue;
      }
    }

    // Check for table header + separator
    if (i + 1 < lines.length && line.includes('|') && isTableSep(lines[i + 1])) {
      const headerCells = parseCells(line);
      const sepCells = parseCells(lines[i + 1]);
      if (headerCells.length > 0 && sepCells.length > 0) {
        const alignments = sepCells.map(getAlign);
        let tbl = '<div class="md-table-wrap"><table><thead><tr>';
        headerCells.forEach((h, col) => {
          const align = alignments[col] ? ` style="text-align: ${alignments[col]};"` : '';
          tbl += `<th${align}>${h}</th>`;
        });
        tbl += '</tr></thead><tbody>';

        let j = i + 2;
        while (j < lines.length) {
          const rowLine = lines[j].trim();
          if (!rowLine || !rowLine.includes('|') || isTableSep(rowLine)) break;
          const rowCells = parseCells(rowLine);
          tbl += '<tr>';
          for (let c = 0; c < headerCells.length; c++) {
            const cellVal = rowCells[c] !== undefined ? rowCells[c] : '';
            const align = alignments[c] ? ` style="text-align: ${alignments[c]};"` : '';
            tbl += `<td${align}>${cellVal}</td>`;
          }
          tbl += '</tr>';
          j++;
        }
        tbl += '</tbody></table></div>';
        output.push(tbl);
        i = j;
        continue;
      }
    }

    if (/^---+$/.test(trimmed) || /^\*\*\*+$/.test(trimmed) || /^___+$/.test(trimmed)) {
      output.push('<hr>');
      i++;
      continue;
    }
    if (/^###\s+(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^###\s+(.+)$/, '<h3 class="md-h3">$1</h3>'));
      i++;
      continue;
    }
    if (/^##\s+(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^##\s+(.+)$/, '<h2 class="md-h2">$1</h2>'));
      i++;
      continue;
    }
    if (/^#\s+(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^#\s+(.+)$/, '<h1 class="md-h1">$1</h1>'));
      i++;
      continue;
    }
    if (/^####\s+(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^####\s+(.+)$/, '<h4 class="md-h4">$1</h4>'));
      i++;
      continue;
    }
    if (/^[\*\-]\s+(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^[\*\-]\s+(.+)$/, '<li class="md-li">$1</li>'));
      i++;
      continue;
    }
    if (/^\d+\.\s+(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^(\d+\.)\s+(.+)$/, '<li class="md-li-num"><b>$1</b> $2</li>'));
      i++;
      continue;
    }
    if (/^>\s*(.+)$/.test(trimmed)) {
      output.push(trimmed.replace(/^>\s*(.+)$/, '<blockquote>$1</blockquote>'));
      i++;
      continue;
    }
    output.push(`<p>${line}</p>`);
    i++;
  }

  let res = output.join('');

  // 6. Restore code blocks
  codeBlocks.forEach((codeHtml, idx) => {
    res = res.replace(new RegExp(`@@OPTCHAT_CODE_${idx}@@`, 'g'), codeHtml);
  });

  // 7. Restore remaining math blocks
  mathBlocks.forEach((b, idx) => {
    res = res.replace(new RegExp(`@@OPTCHAT_MATH_${idx}@@`, 'g'), () => renderMathToken(b.math, b.display));
  });

  return res;
}

// User Actions & Commands
async function sendMessage() {
  if (isRecordingVoice) {
    stopVoiceDictation();
  }
  const input = document.getElementById('prompt-input');
  const text = input ? input.value.trim() : '';
  if (!text) return;

  if (input) {
    input.value = '';
    input.style.height = '46px';
    input.focus();
    setAppHeight();
  }

  // If slash command
  if (text.startsWith('/')) {
    if (text === '/tokens' || text === '/telemetry') {
      openTelemetryDrawer();
      return;
    }
    const bubble = appendMessage('talk', '', new Date().toISOString(), true);
    await handleSlashCommand(text, bubble);
    return;
  }

  const reqId = 'req_' + Date.now() + '_' + Math.random().toString(36).substring(2, 8);
  const targetWs = (selectedWorkspace || 'all').toLowerCase();
  const nowIso = new Date().toISOString();

  // Show user message immediately in chat
  const userBubble = appendMessage('user', text, nowIso, false, null, targetWs !== 'all' ? targetWs : null);
  if (userBubble) {
    const uRow = userBubble.closest('.message-row');
    if (uRow) {
      uRow.setAttribute('data-raw-text', text.trim());
      uRow.setAttribute('data-req-id', reqId);
      uRow.setAttribute('data-workspace', targetWs);
    }
  }

  // Create a pending assistant bubble that will receive stream events
  const bubble = appendMessage('talk', '', nowIso, true, null, targetWs !== 'all' ? targetWs : null);
  if (bubble) {
    bubble.innerHTML = `<span style="color: var(--text-dim); font-style: italic;">⏳ Queued...</span>`;
    const aRow = bubble.closest('.message-row');
    if (aRow) {
      aRow.setAttribute('data-req-id', reqId);
      aRow.setAttribute('data-workspace', targetWs);
    }
  }

  const reqObj = {
    reqId,
    workspace: targetWs,
    model: selectedModel,
    userText: text,
    streamedText: '',
    status: 'Queued...',
    date: nowIso,
    bubble: bubble
  };
  inFlightRequests.set(reqId, reqObj);

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        workspace: targetWs,
        req_id: reqId,
        model: selectedModel
      })
    });
    if (!res.ok) {
      const err = await res.json();
      const errMsg = err.error || 'Failed to submit';
      if (reqObj.bubble && document.body.contains(reqObj.bubble)) {
        reqObj.bubble.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(errMsg)}</span>`;
      }
      inFlightRequests.delete(reqId);
    }
  } catch (err) {
    if (reqObj.bubble && document.body.contains(reqObj.bubble)) {
      reqObj.bubble.innerHTML = `<span style="color: #ef4444;">Network error: ${escapeHtml(err.message)}</span>`;
    }
    inFlightRequests.delete(reqId);
  }
}

async function handleSlashCommand(cmd, bubble) {
  try {
    const res = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd })
    });
    const data = await res.json();
    const idx = pendingAssistantBubbles.indexOf(bubble);
    if (idx !== -1) pendingAssistantBubbles.splice(idx, 1);
    bubble.innerHTML = renderBubbleContent('talk', data.output || 'Command executed.');
    fetchState();
  } catch (e) {
    const idx = pendingAssistantBubbles.indexOf(bubble);
    if (idx !== -1) pendingAssistantBubbles.splice(idx, 1);
    bubble.innerHTML = `<span style="color: #ef4444;">Error running command: ${escapeHtml(e.message)}</span>`;
  }
}

// Drawer management
async function openTreeDrawer() {
  document.getElementById('tree-drawer').style.display = 'block';
  const content = document.getElementById('tree-content');
  content.innerHTML = '<p style="color: var(--text-dim); text-align: center;">Rendering live &lt;chat&gt; view...</p>';

  try {
    const res = await fetch('/api/view');
    const data = await res.json();
    renderTreeLines(data.lines || []);
  } catch (e) {
    content.innerHTML = `<p style="color: #ef4444;">Failed to load view: ${escapeHtml(e.message)}</p>`;
  }
}

function closeTreeDrawer() {
  document.getElementById('tree-drawer').style.display = 'none';
}

function renderTreeLines(lines) {
  const container = document.getElementById('tree-content');
  container.innerHTML = '';
  if (!lines || !lines.length) {
    container.innerHTML = '<p style="color: var(--text-dim);">Tree is empty.</p>';
    return;
  }

  lines.forEach(line => {
    const pipeIdx = line.indexOf('|');
    if (pipeIdx === -1) return;
    const coord = line.slice(0, pipeIdx);
    const text = line.slice(pipeIdx + 1);

    const el = document.createElement('div');
    el.className = 'tree-line';
    el.innerHTML = `<span class="tree-coord">${escapeHtml(coord)}|</span><span class="tree-text">${escapeHtml(text)}</span>`;
    el.onclick = () => openActionSheet(coord, text);
    container.appendChild(el);
  });
}

function openActionSheet(coord, text) {
  selectedLine = { coord, text };
  document.getElementById('action-sheet-title').textContent = `Line: ${coord}`;
  document.getElementById('action-sheet-text').textContent = text.slice(0, 120) + (text.length > 120 ? '...' : '');
  document.getElementById('action-sheet').style.display = 'block';
}

function closeActionSheet() {
  document.getElementById('action-sheet').style.display = 'none';
  selectedLine = null;
}

async function executeZoomAction() {
  if (!selectedLine) return;
  const parts = selectedLine.coord.split('+');
  const id = parseInt(parts[0]);
  const n = parseInt(parts[1]);
  closeActionSheet();
  closeTreeDrawer();

  appendMessage('user', `/zoom ${id} ${n}`, new Date().toISOString());
  try {
    const res = await fetch('/api/zoom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, n })
    });
    const data = await res.json();
    appendMessage('talk', data.output, new Date().toISOString());
  } catch (e) {
    appendMessage('talk', `Zoom failed: ${e.message}`, new Date().toISOString());
  }
}

async function executeDateAction() {
  if (!selectedLine) return;
  const id = parseInt(selectedLine.coord.split('+')[0]);
  closeActionSheet();
  closeTreeDrawer();

  appendMessage('user', `/date ${id}`, new Date().toISOString());
  try {
    const res = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: `/date ${id}` })
    });
    const data = await res.json();
    appendMessage('talk', data.output, new Date().toISOString());
  } catch (e) {
    appendMessage('talk', `Date check failed: ${e.message}`, new Date().toISOString());
  }
}

function copyLineText() {
  if (selectedLine && selectedLine.text) {
    navigator.clipboard.writeText(selectedLine.text);
  }
  closeActionSheet();
}

function openStats() {
  quickSend('/stats');
}

// Voice Dictation & Audio Waveform Visualizer
let isRecordingVoice = false;
let speechRecognizer = null;
let audioContext = null;
let audioStream = null;
let animFrameId = null;
let initialPromptText = '';

async function toggleVoiceDictation() {
  if (isRecordingVoice) {
    stopVoiceDictation();
  } else {
    await startVoiceDictation();
  }
}

async function startVoiceDictation() {
  const micBtn = document.getElementById('btn-mic');
  const voiceBar = document.getElementById('voice-bar');
  const statusText = document.getElementById('voice-status-text');
  const promptInput = document.getElementById('prompt-input');

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition && !navigator.mediaDevices?.getUserMedia) {
    alert("Speech recognition is not supported in this browser.");
    return;
  }

  try {
    // 1. Initialize AudioContext and Analyser for live waveform visualization
    if (navigator.mediaDevices?.getUserMedia) {
      audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioContext.createMediaStreamSource(audioStream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 64;
      source.connect(analyser);

      const canvas = document.getElementById('voice-canvas');
      if (canvas) {
        const ctx = canvas.getContext('2d');
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);

        function drawWaveform() {
          if (!isRecordingVoice) return;
          animFrameId = requestAnimationFrame(drawWaveform);
          analyser.getByteFrequencyData(dataArray);

          ctx.clearRect(0, 0, canvas.width, canvas.height);
          const barWidth = (canvas.width / bufferLength) * 1.5;
          let x = 0;

          for (let i = 0; i < bufferLength; i++) {
            const barHeight = (dataArray[i] / 255) * canvas.height;
            const r = Math.min(255, 120 + dataArray[i]);
            const g = Math.max(30, 180 - dataArray[i]);
            const b = 250;
            ctx.fillStyle = `rgb(${r},${g},${b})`;
            ctx.fillRect(x, (canvas.height - barHeight) / 2, barWidth - 1, Math.max(2, barHeight));
            x += barWidth;
          }
        }
        drawWaveform();
      }
    }

    // 2. Initialize SpeechRecognition if available
    initialPromptText = promptInput ? promptInput.value : '';
    if (initialPromptText && !initialPromptText.endsWith(' ')) {
      initialPromptText += ' ';
    }

    if (SpeechRecognition) {
      speechRecognizer = new SpeechRecognition();
      speechRecognizer.continuous = true;
      speechRecognizer.interimResults = true;
      speechRecognizer.lang = navigator.language || 'en-US';

      speechRecognizer.onresult = (event) => {
        let transcript = '';
        for (let i = 0; i < event.results.length; i++) {
          transcript += event.results[i][0].transcript;
        }
        if (promptInput) {
          promptInput.value = initialPromptText + transcript;
          autoGrow(promptInput);
          promptInput.scrollTop = promptInput.scrollHeight;
        }
      };

      speechRecognizer.onerror = (e) => {
        console.warn('Speech recognition error:', e.error);
        if (e.error === 'not-allowed') {
          if (statusText) statusText.textContent = 'Mic denied';
          setTimeout(stopVoiceDictation, 1500);
        }
      };

      speechRecognizer.onend = () => {
        if (isRecordingVoice) {
          try { speechRecognizer.start(); } catch (_) {}
        }
      };

      speechRecognizer.start();
    }

    isRecordingVoice = true;
    if (micBtn) micBtn.classList.add('recording');
    if (voiceBar) voiceBar.style.display = 'flex';
    if (statusText) statusText.textContent = 'Listening...';
  } catch (err) {
    console.error("Failed to access microphone:", err);
    alert("Microphone permission denied or not available: " + err.message);
    stopVoiceDictation();
  }
}

function stopVoiceDictation() {
  isRecordingVoice = false;

  const micBtn = document.getElementById('btn-mic');
  const voiceBar = document.getElementById('voice-bar');
  if (micBtn) micBtn.classList.remove('recording');
  if (voiceBar) voiceBar.style.display = 'none';

  if (speechRecognizer) {
    try {
      speechRecognizer.onend = null;
      speechRecognizer.stop();
    } catch (_) {}
    speechRecognizer = null;
  }

  if (animFrameId) {
    cancelAnimationFrame(animFrameId);
    animFrameId = null;
  }

  if (audioStream) {
    audioStream.getTracks().forEach(track => track.stop());
    audioStream = null;
  }

  if (audioContext) {
    try { audioContext.close(); } catch (_) {}
    audioContext = null;
  }

  const promptInput = document.getElementById('prompt-input');
  if (promptInput) {
    promptInput.focus();
    autoGrow(promptInput);
  }
}

// Telemetry & Token Analytics
let currentTelemetryDays = 7;

function formatTokens(num) {
  if (num === null || num === undefined) return '0';
  const n = Number(num);
  if (isNaN(n)) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return n.toLocaleString();
}

function attachTokenBadgeToRow(row, tokens) {
  if (!row || !tokens) return;
  const header = row.querySelector('.message-header');
  if (!header) return;
  if (header.querySelector('.token-badge')) return; // already attached

  const badge = document.createElement('span');
  badge.className = 'token-badge';
  const inTok = formatTokens(tokens.input || 0);
  const outTok = formatTokens(tokens.output || 0);
  const cacheTok = formatTokens(tokens.cache_read || 0);
  const cost = tokens.cost_usd != null ? Number(tokens.cost_usd).toFixed(4) : '0.0000';
  const tot = formatTokens(tokens.total || 0);

  badge.title = `Input: ${inTok} | Output: ${outTok} | Cache: ${cacheTok} | Est. Cost: $${cost}`;
  badge.textContent = `⚡ ${tot} ($${cost})`;
  header.appendChild(badge);
}

async function fetchTelemetrySummary() {
  try {
    const res = await fetch('/api/telemetry?days=7');
    if (!res.ok) return;
    const data = await res.json();
    const textEl = document.getElementById('pill-tokens-text');
    if (textEl && data.summary) {
      const tot = formatTokens(data.summary.total_tokens || 0);
      const cost = (data.summary.total_cost_usd || 0).toFixed(2);
      textEl.textContent = `${tot} ($${cost})`;
    }
  } catch (e) {
    console.debug('Failed to fetch telemetry summary:', e);
  }
}

function openTelemetryDrawer() {
  const drawer = document.getElementById('telemetry-drawer');
  if (drawer) {
    drawer.style.display = 'block';
    loadTelemetryData(currentTelemetryDays);
  }
}

function closeTelemetryDrawer() {
  const drawer = document.getElementById('telemetry-drawer');
  if (drawer) {
    drawer.style.display = 'none';
  }
}

function setTelemetryRange(days) {
  currentTelemetryDays = days;
  const pills = document.querySelectorAll('#telemetry-range-pills .range-btn');
  pills.forEach(btn => {
    const btnDays = parseInt(btn.getAttribute('data-days'), 10);
    if (btnDays === days) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });
  loadTelemetryData(days);
}

async function loadTelemetryData(days) {
  let url = `/api/telemetry?days=${days || 0}`;
  if (selectedWorkspace && selectedWorkspace !== 'all') {
    url += `&workspace=${encodeURIComponent(selectedWorkspace)}`;
  }
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderTelemetryUI(data);
  } catch (e) {
    console.error('Failed to load telemetry:', e);
  }
}

function renderTelemetryUI(data) {
  const summary = data.summary || {};

  // Summary Metrics
  const totalTokensEl = document.getElementById('metric-total-tokens');
  const subTokensEl = document.getElementById('metric-sub-tokens');
  const totalCostEl = document.getElementById('metric-total-cost');
  const subCostEl = document.getElementById('metric-sub-cost');
  const totalReqsEl = document.getElementById('metric-total-requests');
  const subReqsEl = document.getElementById('metric-sub-requests');
  const cacheHitEl = document.getElementById('metric-cache-hit');
  const subCacheEl = document.getElementById('metric-sub-cache');

  if (totalTokensEl) totalTokensEl.textContent = formatTokens(summary.total_tokens || 0);
  if (subTokensEl) subTokensEl.textContent = `In: ${formatTokens(summary.total_input_tokens || 0)} · Out: ${formatTokens(summary.total_output_tokens || 0)}`;
  if (totalCostEl) totalCostEl.textContent = `$${(summary.total_cost_usd || 0).toFixed(4)}`;
  if (subCostEl) subCostEl.textContent = `Across ${Array.isArray(data.by_model) ? data.by_model.length : 0} models`;
  if (totalReqsEl) totalReqsEl.textContent = (summary.total_requests || 0).toLocaleString();
  if (subReqsEl) subReqsEl.textContent = `Avg Latency: ${summary.avg_duration_ms ? Math.round(summary.avg_duration_ms) + 'ms' : '-'}`;

  const hitRate = summary.cache_hit_rate != null ? (summary.cache_hit_rate * 100).toFixed(1) + '%' : '0.0%';
  if (cacheHitEl) cacheHitEl.textContent = hitRate;
  if (subCacheEl) subCacheEl.textContent = `Cached: ${formatTokens(summary.total_cache_read_tokens || 0)} tokens`;

  // Daily Chart
  const chartContainer = document.getElementById('telemetry-daily-chart');
  if (chartContainer) {
    const daily = data.daily || [];
    if (!daily.length) {
      chartContainer.innerHTML = '<p style="color: var(--text-dim); text-align: center; width: 100%;">No daily data recorded yet.</p>';
    } else {
      const maxTok = Math.max(...daily.map(d => d.total_tokens || 0), 1);
      chartContainer.innerHTML = daily.map(d => {
        const heightPct = Math.max(Math.round(((d.total_tokens || 0) / maxTok) * 100), 3);
        const costStr = (d.cost_usd || 0).toFixed(4);
        const tooltip = `${d.date}: ${formatTokens(d.total_tokens)} tokens ($${costStr})`;
        const shortDate = d.date.length >= 10 ? d.date.slice(5) : d.date;
        return `
          <div class="chart-bar-group" title="${escapeHtml(tooltip)}">
            <div class="chart-bar-track">
              <div class="chart-bar-fill" style="height: ${heightPct}%;"></div>
            </div>
            <span class="chart-bar-date">${escapeHtml(shortDate)}</span>
          </div>
        `;
      }).join('');
    }
  }

  // By Workspace Table
  const wsTableBody = document.querySelector('#telemetry-ws-table tbody');
  if (wsTableBody) {
    const list = data.by_workspace || [];
    if (!list.length) {
      wsTableBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);">No workspace data</td></tr>';
    } else {
      wsTableBody.innerHTML = list.map(item => `
        <tr>
          <td><strong>${escapeHtml(item.workspace || 'default')}</strong></td>
          <td>${(item.requests || 0).toLocaleString()}</td>
          <td>${formatTokens(item.total_tokens || 0)}</td>
          <td>$${(item.cost_usd || 0).toFixed(4)}</td>
        </tr>
      `).join('');
    }
  }

  // By Model Table
  const modelTableBody = document.querySelector('#telemetry-model-table tbody');
  if (modelTableBody) {
    const list = data.by_model || [];
    if (!list.length) {
      modelTableBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);">No model data</td></tr>';
    } else {
      modelTableBody.innerHTML = list.map(item => `
        <tr>
          <td><strong>${escapeHtml(item.model || 'unknown')}</strong></td>
          <td>${(item.requests || 0).toLocaleString()}</td>
          <td>${formatTokens(item.total_tokens || 0)}</td>
          <td>$${(item.cost_usd || 0).toFixed(4)}</td>
        </tr>
      `).join('');
    }
  }

  // Recent Requests Table
  const recentTableBody = document.querySelector('#telemetry-recent-table tbody');
  if (recentTableBody) {
    const list = data.recent || [];
    if (!list.length) {
      recentTableBody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-dim);">No recent requests</td></tr>';
    } else {
      recentTableBody.innerHTML = list.map(item => {
        const timeStr = formatTimestamp(item.timestamp || item.created_at);
        const inOut = `${formatTokens(item.input_tokens || 0)} / ${formatTokens(item.output_tokens || 0)}`;
        const costStr = `$${(item.cost_usd || 0).toFixed(4)}`;
        const latStr = item.duration_ms ? Math.round(item.duration_ms) + 'ms' : '-';
        return `
          <tr>
            <td>${escapeHtml(timeStr)}</td>
            <td><span class="pill-ws-tag">${escapeHtml(item.workspace || '-')}</span></td>
            <td>${escapeHtml(item.model || '-')}</td>
            <td>${inOut}</td>
            <td>${costStr}</td>
            <td>${latStr}</td>
          </tr>
        `;
      }).join('');
    }
  }
}


