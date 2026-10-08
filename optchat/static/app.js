let selectedLine = null;
let currentAssistantBubble = null;
let currentStreamedText = '';
const pendingAssistantBubbles = [];

function setAppHeight() {
  const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.documentElement.style.setProperty('--app-height', `${h}px`);
  const app = document.getElementById('app');
  if (app) app.style.height = `${h}px`;
}

// Lifecycle and Event Listeners
window.addEventListener('DOMContentLoaded', () => {
  setAppHeight();
  fetchState();
  fetchHistory();
  connectEventStream();
  setInterval(fetchState, 5000);

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

// Subagent Workspace & Tab Bar State
let currentTab = 'main';
const subagentThreads = new Map(); // id -> { id, role, typeName, prompt, status: 'running'|'completed', tools: [], report: null, startTime, endTime }

function renderAgentTabs() {
  const container = document.getElementById('tabs-container');
  if (!container) return;

  let html = `
    <button class="agent-tab ${currentTab === 'main' ? 'active' : ''}" id="tab-btn-main" onclick="switchAgentTab('main')">
      <span class="tab-icon">💬</span>
      <span class="tab-label">Main Chat</span>
    </button>
  `;

  for (const [id, thread] of subagentThreads.entries()) {
    const isActive = (currentTab === id);
    const isRunning = (thread.status === 'running');
    const roleShort = thread.role.length > 20 ? thread.role.slice(0, 18) + '…' : thread.role;

    html += `
      <button class="agent-tab ${isActive ? 'active' : ''}" id="tab-btn-${escapeHtml(id)}" onclick="switchAgentTab('${escapeHtml(id)}')">
        <span class="${isRunning ? 'tab-dot-running' : 'tab-dot-done'}">${isRunning ? '' : '✓'}</span>
        <span class="tab-label">${escapeHtml(roleShort)}</span>
        <span class="tab-badge">${escapeHtml(thread.typeName || 'sub')}</span>
      </button>
    `;
  }

  container.innerHTML = html;
}

function switchAgentTab(tabId) {
  currentTab = tabId;
  renderAgentTabs();

  const chatStream = document.getElementById('chat-stream');
  const subagentStream = document.getElementById('subagent-stream');

  if (tabId === 'main') {
    if (subagentStream) subagentStream.style.display = 'none';
    if (chatStream) {
      chatStream.style.display = 'flex';
      scrollToBottom();
    }
  } else {
    if (chatStream) chatStream.style.display = 'none';
    if (subagentStream) {
      subagentStream.style.display = 'flex';
      renderSubagentView(tabId);
    }
  }
}

function renderSubagentView(tabId) {
  const subagentStream = document.getElementById('subagent-stream');
  if (!subagentStream) return;

  const thread = subagentThreads.get(tabId);
  if (!thread) {
    subagentStream.innerHTML = `
      <div style="padding: 20px; text-align: center; color: var(--text-dim);">
        <p>Subagent workspace not found.</p>
        <button class="subagent-back-btn" onclick="switchAgentTab('main')" style="margin-top: 10px;">← Back to Main Chat</button>
      </div>
    `;
    return;
  }

  const isRunning = (thread.status === 'running');

  let toolsHtml = '';
  if (thread.tools && thread.tools.length > 0) {
    toolsHtml = thread.tools.map((t, idx) => {
      const toolName = t.tool || 'tool';
      const argsStr = typeof t.args === 'object' ? JSON.stringify(t.args, null, 2) : String(t.args || '');
      const preview = argsStr.length > 80 ? argsStr.slice(0, 80) + '...' : argsStr;
      return `
        <details class="tool-box" ${idx === thread.tools.length - 1 && isRunning ? 'open' : ''}>
          <summary>⚡ #${idx + 1} <b>${escapeHtml(toolName)}</b> <span style="opacity: 0.7; font-weight: normal; margin-left: 6px;">${escapeHtml(preview.replace(/\\n/g, ' '))}</span></summary>
          <pre style="margin-top: 6px;">${escapeHtml(argsStr)}</pre>
        </details>
      `;
    }).join('');
  } else {
    toolsHtml = `<p style="font-size: 0.82rem; color: var(--text-dim); font-style: italic; padding: 4px;">No intermediate tool calls recorded yet.</p>`;
  }

  let reportHtml = '';
  if (thread.report) {
    reportHtml = `
      <div class="subagent-report-container">
        <div class="subagent-section-label" style="color: #38bdf8;">📋 Deliverable / Final Report</div>
        <div class="subagent-report-content" style="margin-top: 8px;">
          ${formatMarkdown(thread.report)}
        </div>
      </div>
    `;
  } else if (isRunning) {
    reportHtml = `
      <div style="padding: 12px; background: rgba(16, 185, 129, 0.08); border: 1px dashed rgba(16, 185, 129, 0.3); border-radius: 10px; display: flex; align-items: center; gap: 8px;">
        <span class="tab-dot-running"></span>
        <span style="font-size: 0.84rem; color: #34d399;">Subagent is running in background...</span>
      </div>
    `;
  }

  subagentStream.innerHTML = `
    <div class="subagent-view-header">
      <div class="subagent-view-title">
        <span>🚀 ${escapeHtml(thread.role)}</span>
        <span class="subagent-badge" style="background: rgba(168, 85, 247, 0.25); color: #c084fc;">${escapeHtml(thread.typeName || 'subagent')}</span>
        <span class="subagent-status-pill ${isRunning ? 'running' : 'completed'}">
          ${isRunning ? '<span class="tab-dot-running"></span> In Progress' : '✓ Completed'}
        </span>
      </div>
      <div style="display: flex; align-items: center; gap: 8px;">
        <button class="subagent-back-btn" onclick="switchAgentTab('main')">← Back to Main Chat</button>
      </div>
    </div>

    ${thread.prompt ? `
      <div class="subagent-mission-box">
        <div class="subagent-section-label">🎯 Assigned Mission & Goal</div>
        <div class="subagent-mission-content">${formatMarkdown(thread.prompt)}</div>
      </div>
    ` : ''}

    <div class="subagent-events-container">
      <div class="subagent-section-label">⚡ Execution Activity & Tool Calls (${thread.tools ? thread.tools.length : 0})</div>
      ${toolsHtml}
    </div>

    ${reportHtml}
  `;

  hydratePendingMath();
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

function handleStreamEvent(event) {
  if (event.type === 'token' || event.type === 'text') {
    if (!currentAssistantBubble) {
      currentAssistantBubble = pendingAssistantBubbles.shift() || appendMessage('talk', '', new Date().toISOString(), true);
      currentStreamedText = '';
    }
    currentStreamedText += event.content;
    currentAssistantBubble.innerHTML = formatMarkdown(currentStreamedText) + '<span class="cursor-stream"></span>';
    scrollToBottom();
  } else if (event.type === 'log_talk') {
    if (!currentAssistantBubble) {
      currentAssistantBubble = pendingAssistantBubbles.shift() || appendMessage('talk', '', new Date().toISOString(), false);
    }
    currentStreamedText = event.content;
    currentAssistantBubble.innerHTML = renderBubbleContent('talk', currentStreamedText);
    const row = currentAssistantBubble.closest('.message-row');
    if (row) {
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
    const target = currentAssistantBubble || (pendingAssistantBubbles.length > 0 ? pendingAssistantBubbles[0] : null);
    if (target && !currentStreamedText) {
      target.innerHTML = `<span style="color: var(--text-dim); font-style: italic;">⏳ ${event.content}</span>`;
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
    const typeName = (event.extra && event.extra.typeName) || 'research';
    const prompt = (event.extra && event.extra.prompt) || event.content;
    const cid = (event.extra && event.extra.conversationId) || ('sub-' + Date.now());

    let thread = subagentThreads.get(cid);
    if (!thread) {
      for (const [k, v] of subagentThreads.entries()) {
        if (v.role === role && v.status === 'running') {
          thread = v;
          break;
        }
      }
    }
    if (!thread) {
      thread = {
        id: cid,
        role: role,
        typeName: typeName,
        prompt: prompt,
        status: 'running',
        tools: [],
        report: null,
        startTime: new Date().toISOString(),
        endTime: null
      };
      subagentThreads.set(cid, thread);
    } else {
      thread.role = role;
      thread.typeName = typeName;
      if (prompt) thread.prompt = prompt;
    }
    renderAgentTabs();

    let card = document.getElementById('active-subagent-card');
    if (!card) {
      card = document.createElement('div');
      card.className = 'subagent-card';
      card.id = 'active-subagent-card';
      card.innerHTML = `
        <div class="subagent-header">
          <span>🚀 Subagent: <b>${escapeHtml(role)}</b></span>
          <span class="subagent-badge">${escapeHtml(typeName)}</span>
        </div>
        <div class="subagent-prompt">${formatMarkdown(prompt)}</div>
        <button class="btn-open-tab" onclick="switchAgentTab('${escapeHtml(thread.id)}')">🔍 Open Subagent Workspace Tab →</button>
        <div class="subagent-tools" id="subagent-tools-container"></div>
      `;
      const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
      document.getElementById('chat-stream').insertBefore(card, target);
    } else {
      const badge = card.querySelector('.subagent-badge');
      if (badge) badge.textContent = typeName;
    }
    if (currentTab === thread.id) {
      renderSubagentView(thread.id);
    }
    scrollToBottom();
  } else if (event.type === 'subagent_tool') {
    const cid = (event.extra && event.extra.conversationId);
    const role = (event.extra && event.extra.role);
    let thread = cid ? subagentThreads.get(cid) : null;
    if (!thread && role) {
      for (const [k, v] of subagentThreads.entries()) {
        if (v.role === role) { thread = v; break; }
      }
    }
    if (!thread) {
      for (const [k, v] of subagentThreads.entries()) {
        if (v.status === 'running') { thread = v; break; }
      }
    }
    const toolName = (event.extra && event.extra.tool) || 'tool';
    const toolArgs = (event.extra && event.extra.args) || {};
    if (thread) {
      thread.tools.push({
        tool: toolName,
        args: toolArgs,
        time: new Date().toISOString()
      });
      if (currentTab === thread.id) {
        renderSubagentView(thread.id);
      }
    }
    let toolsContainer = document.getElementById('subagent-tools-container');
    if (!toolsContainer) {
      const toolEl = document.createElement('details');
      toolEl.className = 'tool-box';
      toolEl.style.borderLeftColor = 'var(--purple)';
      toolEl.innerHTML = `<summary>⚡ ${escapeHtml(event.content)}</summary><pre>${escapeHtml(JSON.stringify(event.extra || {}, null, 2))}</pre>`;
      const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
      document.getElementById('chat-stream').insertBefore(toolEl, target);
    } else {
      const toolItem = document.createElement('details');
      toolItem.className = 'subagent-tool-item';
      const argsPreview = JSON.stringify(toolArgs);
      toolItem.innerHTML = `<summary>⚡ ${escapeHtml(toolName)}: ${escapeHtml(argsPreview.slice(0, 90))}${argsPreview.length > 90 ? '...' : ''}</summary><pre>${escapeHtml(JSON.stringify(event.extra || {}, null, 2))}</pre>`;
      toolsContainer.appendChild(toolItem);
    }
    scrollToBottom();
  } else if (event.type === 'subagent_complete') {
    const cid = (event.extra && event.extra.conversationId);
    const role = (event.extra && event.extra.role);
    let thread = cid ? subagentThreads.get(cid) : null;
    if (!thread && role) {
      for (const [k, v] of subagentThreads.entries()) {
        if (v.role === role) { thread = v; break; }
      }
    }
    if (thread) {
      thread.status = 'completed';
      thread.endTime = new Date().toISOString();
      renderAgentTabs();
      if (currentTab === thread.id) {
        renderSubagentView(thread.id);
      }
    }
    const card = document.getElementById('active-subagent-card');
    if (card) {
      card.removeAttribute('id');
      const badge = card.querySelector('.subagent-badge');
      if (badge) {
        badge.textContent = 'Completed';
        badge.style.background = 'rgba(16, 185, 129, 0.25)';
        badge.style.color = '#34d399';
      }
    }
    scrollToBottom();
  } else if (event.type === 'subagent_report') {
    const repText = (event.extra && event.extra.report) || event.content;
    const role = (event.extra && event.extra.role) || 'Subagent';
    const cid = (event.extra && event.extra.conversationId);
    let thread = cid ? subagentThreads.get(cid) : null;
    if (!thread && role) {
      for (const [k, v] of subagentThreads.entries()) {
        if (v.role === role) { thread = v; break; }
      }
    }
    if (!thread) {
      const genId = cid || ('sub-' + Date.now());
      thread = {
        id: genId,
        role: role,
        typeName: 'subagent',
        prompt: '',
        status: 'completed',
        tools: [],
        report: repText,
        startTime: new Date().toISOString(),
        endTime: new Date().toISOString()
      };
      subagentThreads.set(genId, thread);
    } else {
      thread.report = repText;
      thread.status = 'completed';
      thread.endTime = new Date().toISOString();
    }
    renderAgentTabs();
    if (currentTab === thread.id) {
      renderSubagentView(thread.id);
    }
    appendMessage('work', `### 📋 Subagent Report: ${role}\n\n${repText}`, new Date().toISOString(), false, thread.id);
    scrollToBottom();
    fetchState();
  } else if (event.type === 'error') {
    if (currentAssistantBubble) {
      currentAssistantBubble.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(event.content)}</span>`;
      currentAssistantBubble = null;
    } else if (pendingAssistantBubbles.length > 0) {
      const pb = pendingAssistantBubbles.shift();
      pb.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(event.content)}</span>`;
    }
  } else if (event.type === 'turn_complete') {
    if (currentAssistantBubble) {
      if (currentStreamedText) {
        currentAssistantBubble.innerHTML = renderBubbleContent('talk', currentStreamedText);
      }
      currentAssistantBubble = null;
      currentStreamedText = '';
    }
    fetchState();
  }
}

let currentWorkspace = '';

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('pill-msgs').textContent = `Msgs: ${data.messages_count.toLocaleString()}`;
    document.getElementById('pill-nodes').textContent = `Nodes: ${data.tree_nodes_count.toLocaleString()}`;
    const pct = ((data.view_size / data.view_budget) * 100).toFixed(0);
    document.getElementById('pill-view').textContent = `View: ${pct}%`;
    document.getElementById('status-dot').style.background = data.is_settled ? '#10b981' : '#f59e0b';
    if (data.workspace) {
      currentWorkspace = data.workspace;
      const parts = data.workspace.split('/').filter(Boolean);
      const name = parts.length > 0 ? parts[parts.length - 1] : '/';
      const wsEl = document.getElementById('pill-ws');
      if (wsEl) wsEl.textContent = '📁 ' + name;
    }
  } catch (e) {
    document.getElementById('status-dot').style.background = '#ef4444';
  }
}

async function openSessionModal() {
  const newWs = prompt("Active Workspace:\n" + currentWorkspace + "\n\nEnter new workspace path to switch (e.g. /home/simonpure):", currentWorkspace);
  if (newWs && newWs.trim() && newWs.trim() !== currentWorkspace) {
    try {
      const res = await fetch('/api/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workspace: newWs.trim() })
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
    const res = await fetch('/api/history?limit=50');
    if (!res.ok) {
      console.error('Failed to load history:', res.status, res.statusText);
      return;
    }
    const msgs = await res.json();
    const stream = document.getElementById('chat-stream');
    if (Array.isArray(msgs) && msgs.length > 0) {
      stream.innerHTML = '';

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

      msgs.forEach((m, idx) => {
        let threadId = null;
        if (m.kind === 'work' && (m.text.includes('Subagent Report:') || m.text.startsWith('['))) {
          let role = 'Subagent';
          let reportText = m.text;
          const match = m.text.match(/Subagent Report:?\s*([^\n\r]+)/i);
          if (match) {
            role = match[1].trim();
            reportText = m.text.replace(/### 📋 Subagent Report:\s*[^\n]+\n*/i, '').trim();
          } else {
            const match2 = m.text.match(/^\[([^\]]+)\]\s*(.*)/s);
            if (match2) {
              role = match2[1].trim();
              reportText = match2[2].trim();
            }
          }
          threadId = 'hist-sub-' + idx;
          if (!subagentThreads.has(threadId)) {
            subagentThreads.set(threadId, {
              id: threadId,
              role: role,
              typeName: 'subagent',
              prompt: '',
              status: 'completed',
              tools: [],
              report: reportText,
              startTime: m.date || new Date().toISOString(),
              endTime: m.date || new Date().toISOString()
            });
          }
        }
        appendMessage(m.kind, m.text, m.date, false, threadId);
      });
      renderAgentTabs();
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
    const res = await fetch('/api/view');
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
  if (kind === 'work' && threadId) {
    fullHtml += `<div style="margin-top: 10px;"><button class="btn-open-tab" onclick="switchAgentTab('${escapeHtml(threadId)}')">🔍 Switch to Subagent Workspace Tab →</button></div>`;
  }

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

function appendMessage(kind, text, dateStr, isStreaming = false, threadId = null) {
  const stream = document.getElementById('chat-stream');
  if (!stream) return null;

  const row = document.createElement('div');
  row.className = `message-row ${kind}`;

  const header = document.createElement('div');
  header.className = 'message-header';

  const sender = document.createElement('span');
  sender.className = 'message-sender';
  sender.textContent = getSenderLabel(kind, text);
  header.appendChild(sender);

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

  // Markdown links: [title](url)
  escaped = escaped.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" style="color: var(--accent); text-decoration: underline;">$1</a>');

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
  const input = document.getElementById('prompt-input');
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  input.style.height = '46px';
  input.focus();
  setAppHeight();

  // Show user message immediately in chat
  appendMessage('user', text, new Date().toISOString());

  // Create a pending assistant bubble that will receive stream events
  const bubble = appendMessage('talk', '', new Date().toISOString(), true);
  bubble.innerHTML = `<span style="color: var(--text-dim); font-style: italic;">⏳ Queued...</span>`;
  pendingAssistantBubbles.push(bubble);

  // If slash command
  if (text.startsWith('/')) {
    await handleSlashCommand(text, bubble);
    return;
  }

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    });
    if (!res.ok) {
      const err = await res.json();
      bubble.innerHTML = `<span style="color: #ef4444;">Error: ${escapeHtml(err.error || 'Failed to submit')}</span>`;
      const idx = pendingAssistantBubbles.indexOf(bubble);
      if (idx !== -1) pendingAssistantBubbles.splice(idx, 1);
    }
  } catch (err) {
    bubble.innerHTML = `<span style="color: #ef4444;">Network error: ${escapeHtml(err.message)}</span>`;
    const idx = pendingAssistantBubbles.indexOf(bubble);
    if (idx !== -1) pendingAssistantBubbles.splice(idx, 1);
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
