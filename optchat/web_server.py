"""Responsive Web Interface for OptChat (Mobile & Desktop).

Provides a local intranet web version of the CLI with:
- Real-time token streaming via Server-Sent Events (SSE)
- Mobile-first responsive UI (touch friendly, dynamic viewport)
- Interactive <chat> live view with Zoom & Date line actions
- Direct access to browse.html tree visualization
- Built-in slash commands (/view, /stats, /browse, /zoom, /date)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import socket
import sys
from typing import Any, Dict, List, Optional, Set

from aiohttp import web

from optchat.agent import TurnAgent
from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.visualizer import export_html_to_file
from optchat.providers import create_provider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.tree import node_coords
from optchat.view import LiveView

logger = logging.getLogger("optchat.web")

# Embedded, self-contained HTML/CSS/JS frontend (no external CDN required)
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>OptChat Web</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: #131b2e;
      --card-border: #1e293b;
      --card-hover: #1e2d4a;
      --text: #f1f5f9;
      --text-dim: #94a3b8;
      --text-muted: #64748b;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --accent-bg: rgba(56, 189, 248, 0.12);
      --user-bg: #1e3a8a;
      --user-border: #2563eb;
      --code-bg: #0a0f1d;
      --green: #10b981;
      --yellow: #f59e0b;
      --purple: #a855f7;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    html, body {
      height: 100%;
      height: 100dvh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      overflow: hidden;
    }
    #app {
      display: flex;
      flex-direction: column;
      height: 100%;
      max-width: 900px;
      margin: 0 auto;
      position: relative;
    }

    /* Header */
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 16px;
      background: var(--card-bg);
      border-bottom: 1px solid var(--card-border);
      flex-shrink: 0;
      gap: 12px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
      font-size: 1.1rem;
      letter-spacing: -0.02em;
    }
    .brand-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 8px var(--green);
    }
    .stats-pills {
      display: flex;
      gap: 6px;
      overflow-x: auto;
      font-size: 0.75rem;
      color: var(--text-dim);
    }
    .pill {
      background: rgba(255,255,255,0.06);
      padding: 4px 8px;
      border-radius: 12px;
      white-space: nowrap;
      cursor: pointer;
    }
    .pill:hover { background: rgba(255,255,255,0.12); }
    .header-actions {
      display: flex;
      gap: 6px;
    }
    .btn-icon {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 6px 10px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.85rem;
      display: flex;
      align-items: center;
      gap: 4px;
      transition: background 0.15s;
    }
    .btn-icon:hover { background: var(--card-hover); }

    /* Main Chat Stream */
    #chat-stream {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      scroll-behavior: smooth;
    }
    .message-row {
      display: flex;
      flex-direction: column;
      gap: 4px;
      width: 100%;
    }
    .message-row.user { align-items: flex-end; }
    .message-row.talk, .message-row.tool { align-items: flex-start; }

    .message-sender {
      font-size: 0.72rem;
      color: var(--text-muted);
      padding: 0 4px;
    }
    .bubble {
      max-width: 88%;
      padding: 12px 16px;
      border-radius: 16px;
      font-size: 0.95rem;
      line-height: 1.5;
      word-break: break-word;
    }
    .user .bubble {
      background: var(--user-bg);
      border: 1px solid var(--user-border);
      color: #fff;
      border-bottom-right-radius: 4px;
    }
    .talk .bubble {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      border-bottom-left-radius: 4px;
    }
    .note .bubble {
      background: rgba(168, 85, 247, 0.15);
      border: 1px solid rgba(168, 85, 247, 0.3);
      color: #e9d5ff;
      font-size: 0.85rem;
    }
    .tool-box {
      font-size: 0.82rem;
      background: #0f172a;
      border: 1px solid #1e293b;
      border-left: 3px solid var(--yellow);
      padding: 8px 12px;
      border-radius: 8px;
      width: 100%;
      max-width: 90%;
      margin: 4px 0;
      color: #cbd5e1;
      cursor: pointer;
    }
    .tool-box summary { font-weight: 600; outline: none; }
    .tool-box pre { margin-top: 6px; font-family: monospace; white-space: pre-wrap; font-size: 0.78rem; color: #94a3b8; }

    /* Markdown inside bubbles */
    .bubble p { margin-bottom: 8px; }
    .bubble p:last-child { margin-bottom: 0; }
    .bubble code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      background: rgba(0, 0, 0, 0.3);
      padding: 2px 5px;
      border-radius: 4px;
      font-size: 0.88em;
    }
    .bubble pre {
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      padding: 10px;
      border-radius: 8px;
      overflow-x: auto;
      margin: 8px 0;
    }
    .bubble pre code { background: none; padding: 0; }
    .bubble hr {
      border: 0;
      border-top: 1px solid var(--card-border);
      margin: 14px 0;
      opacity: 0.8;
    }
    .bubble h1, .bubble h2, .bubble h3, .bubble h4 {
      font-weight: 700;
      margin: 14px 0 6px;
      line-height: 1.3;
    }
    .bubble h1 { font-size: 1.25rem; color: #f8fafc; }
    .bubble h2 { font-size: 1.15rem; color: #f1f5f9; }
    .bubble h3 { font-size: 1.05rem; color: var(--accent); }
    .bubble h4 { font-size: 0.95rem; color: #94a3b8; }
    .bubble ul, .bubble ol { margin: 8px 0 8px 20px; }
    .bubble li { margin-bottom: 4px; }
    .bubble li.md-li { margin-left: 18px; margin-bottom: 4px; list-style-type: disc; }
    .bubble li.md-li-num { margin-left: 18px; margin-bottom: 4px; list-style-type: none; }
    .bubble blockquote {
      border-left: 3px solid var(--accent);
      padding-left: 10px;
      color: var(--text-dim);
      margin: 8px 0;
    }

    .cursor-stream {
      display: inline-block;
      width: 8px;
      height: 15px;
      background: var(--accent);
      vertical-align: middle;
      animation: blink 0.8s infinite;
      margin-left: 4px;
    }
    @keyframes blink { 0%, 50% { opacity: 1; } 51%, 100% { opacity: 0; } }

    /* Input area */
    footer {
      background: var(--card-bg);
      border-top: 1px solid var(--card-border);
      padding: 10px 16px;
      padding-bottom: calc(10px + env(safe-area-inset-bottom, 0px));
      flex-shrink: 0;
    }
    .quick-bar {
      display: flex;
      gap: 6px;
      margin-bottom: 8px;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .quick-bar::-webkit-scrollbar { display: none; }
    .quick-pill {
      font-size: 0.72rem;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.1);
      color: var(--text-dim);
      padding: 3px 8px;
      border-radius: 6px;
      cursor: pointer;
      white-space: nowrap;
    }
    .quick-pill:hover { background: rgba(255,255,255,0.1); color: #fff; }
    .input-form {
      display: flex;
      gap: 8px;
      align-items: flex-end;
    }
    #prompt-input {
      flex: 1;
      background: var(--bg);
      border: 1px solid var(--card-border);
      color: #fff;
      font-size: 0.95rem;
      border-radius: 12px;
      padding: 10px 14px;
      resize: none;
      height: 44px;
      max-height: 140px;
      outline: none;
      line-height: 1.4;
      font-family: inherit;
    }
    #prompt-input:focus { border-color: var(--accent); }
    #send-btn {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: var(--accent);
      border: none;
      color: #000;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      flex-shrink: 0;
      transition: background 0.15s, transform 0.05s;
    }
    #send-btn:active { transform: scale(0.95); }
    #send-btn:disabled { background: var(--card-border); color: var(--text-muted); cursor: not-allowed; }

    /* Modal / Drawer */
    .drawer-overlay {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.7);
      backdrop-filter: blur(4px);
      display: none;
      z-index: 100;
    }
    .drawer {
      position: absolute;
      bottom: 0; left: 0; right: 0;
      height: 85%;
      max-height: 700px;
      background: var(--card-bg);
      border-top: 1px solid var(--card-border);
      border-top-left-radius: 16px;
      border-top-right-radius: 16px;
      display: flex;
      flex-direction: column;
      box-shadow: 0 -10px 30px rgba(0,0,0,0.5);
    }
    .drawer-header {
      padding: 12px 16px;
      border-bottom: 1px solid var(--card-border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 700;
    }
    .drawer-content {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.85rem;
    }
    .tree-line {
      padding: 6px 8px;
      border-radius: 6px;
      margin-bottom: 4px;
      cursor: pointer;
      display: flex;
      gap: 8px;
      transition: background 0.1s;
      border: 1px solid transparent;
    }
    .tree-line:hover {
      background: var(--card-hover);
      border-color: rgba(56, 189, 248, 0.3);
    }
    .tree-coord {
      color: var(--accent);
      font-weight: 600;
      white-space: nowrap;
    }
    .tree-text {
      color: #cbd5e1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
    }

    /* Action sheet for selected line */
    .action-sheet {
      position: absolute;
      bottom: 0; left: 0; right: 0;
      background: #1e293b;
      border-top: 1px solid #334155;
      padding: 16px;
      border-top-left-radius: 16px;
      border-top-right-radius: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      box-shadow: 0 -10px 25px rgba(0,0,0,0.6);
      z-index: 200;
    }
    .action-btn {
      padding: 12px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      font-size: 0.95rem;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      text-align: left;
    }
    .action-btn:hover { background: var(--card-hover); color: var(--accent); }
    .action-cancel { background: transparent; border-color: transparent; text-align: center; color: var(--text-dim); }

    @media (min-width: 768px) {
      .drawer {
        width: 550px;
        right: 0;
        left: auto;
        top: 0;
        bottom: 0;
        height: 100%;
        max-height: 100%;
        border-top-left-radius: 0;
        border-left: 1px solid var(--card-border);
      }
    }
  </style>
</head>
<body>
  <div id="app">
    <!-- Header -->
    <header>
      <div class="brand">
        <div class="brand-dot" id="status-dot"></div>
        <span>OptChat</span>
      </div>
      <div class="stats-pills" id="stats-bar" onclick="openStats()">
        <span class="pill" id="pill-msgs">Msgs: -</span>
        <span class="pill" id="pill-nodes">Nodes: -</span>
        <span class="pill" id="pill-view">View: -</span>
      </div>
      <div class="header-actions">
        <button class="btn-icon" onclick="openTreeDrawer()">📋 <span class="hide-sm">View</span></button>
        <button class="btn-icon" onclick="window.open('/browse', '_blank')">🌳 <span class="hide-sm">Browse</span></button>
      </div>
    </header>

    <!-- Chat Messages -->
    <div id="chat-stream">
      <div class="message-row talk">
        <span class="message-sender">OptChat</span>
        <div class="bubble">
          👋 Connected to OptChat memory engine. Type a message or try <code>/view</code>, <code>/stats</code>, or <code>/browse</code>.
        </div>
      </div>
    </div>

    <!-- Input Footer -->
    <footer>
      <div class="quick-bar">
        <span class="quick-pill" onclick="quickSend('/view')">/view</span>
        <span class="quick-pill" onclick="quickSend('/stats')">/stats</span>
        <span class="quick-pill" onclick="window.open('/browse', '_blank')">/browse</span>
        <span class="quick-pill" onclick="quickSend('/rebuild')">/rebuild</span>
      </div>
      <form class="input-form" onsubmit="event.preventDefault(); sendMessage();">
        <textarea id="prompt-input" rows="1" placeholder="Type a message or /command..." onkeydown="handleKeyDown(event)" oninput="autoGrow(this)"></textarea>
        <button type="submit" id="send-btn">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="22" y1="2" x2="11" y2="13"></line>
            <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
          </svg>
        </button>
      </form>
    </footer>

    <!-- Tree Drawer -->
    <div class="drawer-overlay" id="tree-drawer">
      <div class="drawer">
        <div class="drawer-header">
          <span>Compressed Memory Tree (&lt;chat&gt;)</span>
          <button class="btn-icon" onclick="closeTreeDrawer()">✕</button>
        </div>
        <div class="drawer-content" id="tree-content">
          <p style="color: var(--text-dim); text-align: center;">Loading tree view...</p>
        </div>
      </div>
    </div>

    <!-- Action Sheet for Line Zoom/Date -->
    <div class="drawer-overlay" id="action-sheet" onclick="closeActionSheet()">
      <div class="action-sheet" onclick="event.stopPropagation()">
        <p style="font-size: 0.8rem; color: var(--accent); font-weight: 700;" id="action-sheet-title">Line: 0+1</p>
        <p style="font-size: 0.85rem; color: var(--text-dim); margin-bottom: 6px;" id="action-sheet-text">Preview...</p>
        <button class="action-btn" onclick="executeZoomAction()">🔍 Zoom Line (finer details / verbatim)</button>
        <button class="action-btn" onclick="executeDateAction()">📅 View Date / Timestamp</button>
        <button class="action-btn" onclick="copyLineText()">📋 Copy Text</button>
        <button class="action-btn action-cancel" onclick="closeActionSheet()">Cancel</button>
      </div>
    </div>
  </div>

  <script>
    let selectedLine = null;
    let currentAssistantBubble = null;
    let currentStreamedText = '';
    const pendingAssistantBubbles = [];

    // Load initial state, history & connect persistent event stream
    window.addEventListener('DOMContentLoaded', () => {
      fetchState();
      fetchHistory();
      connectEventStream();
      setInterval(fetchState, 5000);
    });

    function connectEventStream() {
      const evtSource = new EventSource('/api/stream');

      evtSource.onmessage = function(e) {
        try {
          const event = JSON.parse(e.data);
          handleStreamEvent(event);
        } catch (err) {}
      };

      evtSource.onerror = function() {
        evtSource.close();
        setTimeout(connectEventStream, 2000);
      };
    }

    function handleStreamEvent(event) {
      if (event.type === 'token' || event.type === 'text') {
        if (!currentAssistantBubble) {
          currentAssistantBubble = pendingAssistantBubbles.shift() || appendMessage('talk', '', '', true);
          currentStreamedText = '';
        }
        currentStreamedText += event.content;
        currentAssistantBubble.innerHTML = formatMarkdown(currentStreamedText) + '<span class="cursor-stream"></span>';
        scrollToBottom();
      } else if (event.type === 'log_talk') {
        if (!currentAssistantBubble) {
          currentAssistantBubble = pendingAssistantBubbles.shift() || appendMessage('talk', '', '', false);
        }
        currentStreamedText = event.content;
        currentAssistantBubble.innerHTML = formatMarkdown(currentStreamedText);
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
        toolEl.innerHTML = `<summary>⚡ Tool: ${event.content}</summary><pre>${event.content}</pre>`;
        const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
        document.getElementById('chat-stream').insertBefore(toolEl, target);
        scrollToBottom();
      } else if (event.type === 'log_echo') {
        const echoEl = document.createElement('details');
        echoEl.className = 'tool-box';
        echoEl.innerHTML = `<summary>📄 Tool Result</summary><pre>${event.content}</pre>`;
        const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
        document.getElementById('chat-stream').insertBefore(echoEl, target);
        scrollToBottom();
      } else if (event.type === 'error') {
        if (currentAssistantBubble) {
          currentAssistantBubble.innerHTML = `<span style="color: #ef4444;">Error: ${event.content}</span>`;
          currentAssistantBubble = null;
        } else if (pendingAssistantBubbles.length > 0) {
          const pb = pendingAssistantBubbles.shift();
          pb.innerHTML = `<span style="color: #ef4444;">Error: ${event.content}</span>`;
        }
      } else if (event.type === 'turn_complete') {
        if (currentAssistantBubble) {
          if (currentStreamedText) {
            currentAssistantBubble.innerHTML = formatMarkdown(currentStreamedText);
          }
          currentAssistantBubble = null;
          currentStreamedText = '';
        }
        fetchState();
      }
    }

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
      } catch (e) {
        document.getElementById('status-dot').style.background = '#ef4444';
      }
    }

    async function fetchHistory() {
      try {
        const res = await fetch('/api/history?limit=30');
        if (!res.ok) return;
        const msgs = await res.json();
        const stream = document.getElementById('chat-stream');
        if (msgs.length > 0) {
          stream.innerHTML = '';
          msgs.forEach(m => appendMessage(m.kind, m.text, m.date, false));
          scrollToBottom();
        }
      } catch (e) {}
    }

    function scrollToBottom() {
      const stream = document.getElementById('chat-stream');
      stream.scrollTop = stream.scrollHeight;
    }

    function autoGrow(textarea) {
      textarea.style.height = '44px';
      textarea.style.height = Math.min(textarea.scrollHeight, 140) + 'px';
    }

    function handleKeyDown(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    }

    function quickSend(cmd) {
      document.getElementById('prompt-input').value = cmd;
      sendMessage();
    }

    function appendMessage(kind, text, dateStr, isStreaming = false) {
      const stream = document.getElementById('chat-stream');
      const row = document.createElement('div');
      row.className = `message-row ${kind}`;
      
      const sender = document.createElement('span');
      sender.className = 'message-sender';
      sender.textContent = kind === 'user' ? 'You' : (kind === 'talk' ? 'OptChat' : kind.toUpperCase());
      
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.innerHTML = formatMarkdown(text) + (isStreaming ? '<span class="cursor-stream"></span>' : '');

      row.appendChild(sender);
      row.appendChild(bubble);
      stream.appendChild(row);
      scrollToBottom();
      return bubble;
    }

    // Lightweight markdown formatter
    function formatMarkdown(text) {
      if (!text) return '';
      let escaped = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
      
      // Code blocks
      escaped = escaped.replace(/```([a-z0-9_-]*)\\n([\\s\\S]*?)```/gi, (match, lang, code) => {
        return `<pre><code>${code.trim()}</code></pre>`;
      });

      // Inline code
      escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

      // Bold & Italic
      escaped = escaped.replace(/\\*\\*([^\\*]+)\\*\\*/g, '<strong>$1</strong>');
      escaped = escaped.replace(/\\*([^\\*]+)\\*/g, '<em>$1</em>');

      // Markdown links: [title](url)
      escaped = escaped.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\)]+)\\)/g, '<a href="$2" target="_blank" style="color: var(--accent); text-decoration: underline;">$1</a>');

      // Plain URLs
      escaped = escaped.replace(/(^|[^"'>])(https?:\\/\\/[^\\s<]+)/g, '$1<a href="$2" target="_blank" style="color: var(--accent);">$2</a>');

      // Paragraphs, headers, rules, lists, quotes
      const lines = escaped.split('\\n');
      return lines.map(l => {
        const trimmed = l.trim();
        if (!trimmed) return '<br>';
        if (/^---+$/.test(trimmed) || /^\\*\\*\\*+$/.test(trimmed) || /^___+$/.test(trimmed)) {
          return '<hr>';
        }
        if (/^###\\s+(.+)$/.test(trimmed)) {
          return trimmed.replace(/^###\\s+(.+)$/, '<h3 class="md-h3">$1</h3>');
        }
        if (/^##\\s+(.+)$/.test(trimmed)) {
          return trimmed.replace(/^##\\s+(.+)$/, '<h2 class="md-h2">$1</h2>');
        }
        if (/^#\\s+(.+)$/.test(trimmed)) {
          return trimmed.replace(/^#\\s+(.+)$/, '<h1 class="md-h1">$1</h1>');
        }
        if (/^####\\s+(.+)$/.test(trimmed)) {
          return trimmed.replace(/^####\\s+(.+)$/, '<h4 class="md-h4">$1</h4>');
        }
        if (/^[\\*\\-]\\s+(.+)$/.test(trimmed)) {
          return trimmed.replace(/^[\\*\\-]\\s+(.+)$/, '<li class="md-li">$1</li>');
        }
        if (/^\\d+\\.\\s+(.+)$/.test(trimmed)) {
          return trimmed.replace(/^(\\d+\\.)\\s+(.+)$/, '<li class="md-li-num"><b>$1</b> $2</li>');
        }
        if (/^>\\s*(.+)$/.test(trimmed)) {
          return trimmed.replace(/^>\\s*(.+)$/, '<blockquote>$1</blockquote>');
        }
        return `<p>${l}</p>`;
      }).join('');
    }

    async function sendMessage() {
      const input = document.getElementById('prompt-input');
      const text = input.value.trim();
      if (!text) return;

      input.value = '';
      input.style.height = '44px';
      input.focus();

      // Show user message immediately in chat
      appendMessage('user', text, new Date().toISOString());

      // Create a pending assistant bubble that will receive stream events
      const bubble = appendMessage('talk', '', '', true);
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
          bubble.innerHTML = `<span style="color: #ef4444;">Error: ${err.error || 'Failed to submit'}</span>`;
          const idx = pendingAssistantBubbles.indexOf(bubble);
          if (idx !== -1) pendingAssistantBubbles.splice(idx, 1);
        }
      } catch (err) {
        bubble.innerHTML = `<span style="color: #ef4444;">Network error: ${err.message}</span>`;
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
        bubble.innerHTML = formatMarkdown(data.output || 'Command executed.');
        fetchState();
      } catch (e) {
        const idx = pendingAssistantBubbles.indexOf(bubble);
        if (idx !== -1) pendingAssistantBubbles.splice(idx, 1);
        bubble.innerHTML = `<span style="color: #ef4444;">Error running command: ${e.message}</span>`;
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
        content.innerHTML = `<p style="color: #ef4444;">Failed to load view: ${e.message}</p>`;
      }
    }

    function closeTreeDrawer() {
      document.getElementById('tree-drawer').style.display = 'none';
    }

    function renderTreeLines(lines) {
      const container = document.getElementById('tree-content');
      container.innerHTML = '';
      if (!lines.length) {
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
        el.innerHTML = `<span class="tree-coord">${coord}|</span><span class="tree-text">${text}</span>`;
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
  </script>
</body>
</html>
"""


class OptChatWebServer:
    def __init__(
        self,
        chat_dir: Path,
        provider_name: str = "agy",
        model: Optional[str] = None,
        compactor_model: Optional[str] = None,
        timeout: float = 300.0,
    ):
        self.chat_dir = Path(chat_dir).resolve()
        self.provider_name = provider_name
        self.model = model
        self.compactor_model = compactor_model
        self.timeout = timeout

        self.storage: Optional[Storage] = None
        self.view: Optional[LiveView] = None
        self.compactor: Optional[Compactor] = None
        self.agent: Optional[TurnAgent] = None
        self.app = web.Application()

        self._active_stream_queues: Set[asyncio.Queue[Dict[str, Any]]] = set()

    async def init_engine(self) -> None:
        self.storage = Storage(self.chat_dir)
        self.storage.open()

        self.view = LiveView(self.storage)
        self.view.rebuild()

        main_provider = create_provider(self.provider_name, model=self.model, timeout=self.timeout)
        comp_provider = (
            main_provider
            if self.compactor_model is None
            else create_provider(self.provider_name, model=self.compactor_model, timeout=self.timeout)
        )

        self.compactor = Compactor(self.storage, self.view, comp_provider)
        self.compactor.start()

        tool_registry = ToolRegistry()

        def ui_listener(event_type: str, content: str) -> None:
            # Broadcast to active SSE listeners
            event_obj = {"type": event_type, "content": content}
            for q in list(self._active_stream_queues):
                try:
                    q.put_nowait(event_obj)
                except Exception:
                    pass

        agents_md = self.chat_dir.parent / "AGENTS.md"
        self.agent = TurnAgent(
            storage=self.storage,
            view=self.view,
            compactor=self.compactor,
            provider=main_provider,
            tool_registry=tool_registry,
            agents_md_path=agents_md if agents_md.exists() else None,
            git_auto_commit=False,
            ui_callback=ui_listener,
        )

        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/browse", self.handle_browse)
        self.app.router.add_get("/api/state", self.handle_api_state)
        self.app.router.add_get("/api/history", self.handle_api_history)
        self.app.router.add_get("/api/view", self.handle_api_view)
        self.app.router.add_get("/api/stream", self.handle_api_stream)
        self.app.router.add_post("/api/chat", self.handle_api_chat)
        self.app.router.add_post("/api/command", self.handle_api_command)
        self.app.router.add_post("/api/zoom", self.handle_api_zoom)

    async def handle_api_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._active_stream_queues.add(queue)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    payload = f"data: {json.dumps(event)}\n\n"
                    await response.write(payload.encode("utf-8"))
                except asyncio.TimeoutError:
                    await response.write(b": keep-alive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._active_stream_queues.discard(queue)
            try:
                await response.write_eof()
            except Exception:
                pass

        return response

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML_PAGE, content_type="text/html")

    async def handle_browse(self, request: web.Request) -> web.Response:
        assert self.storage is not None and self.view is not None
        browse_file = self.storage.chat_dir / "browse.html"
        if not browse_file.exists():
            export_html_to_file(self.storage, self.view, browse_file)
        if browse_file.exists():
            return web.FileResponse(browse_file)
        return web.Response(text="browse.html not available yet", status=404)

    async def handle_api_state(self, request: web.Request) -> web.Response:
        assert self.storage is not None and self.view is not None
        return web.json_response({
            "messages_count": len(self.storage.messages),
            "tree_nodes_count": len(self.storage.tree),
            "view_size": self.view.compute_size(),
            "view_budget": self.view.budget,
            "is_settled": self.view.is_settled(),
        })

    async def handle_api_history(self, request: web.Request) -> web.Response:
        assert self.storage is not None
        limit_str = request.query.get("limit", "50")
        try:
            limit = int(limit_str)
        except ValueError:
            limit = 50

        msgs = self.storage.messages[-limit:] if limit > 0 else self.storage.messages
        result = [
            {
                "i": m.i,
                "kind": m.kind,
                "text": m.text,
                "size": m.size,
                "date": m.date,
            }
            for m in msgs
            if m.kind in ("user", "talk", "note")
        ]
        return web.json_response(result)

    async def handle_api_view(self, request: web.Request) -> web.Response:
        assert self.storage is not None and self.view is not None
        lines = [part.render(self.storage) for part in self.view.parts]
        return web.json_response({
            "size": self.view.compute_size(),
            "budget": self.view.budget,
            "lines": lines,
        })

    async def handle_api_chat(self, request: web.Request) -> web.Response:
        assert self.agent is not None
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Message cannot be empty"}, status=400)

        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept:
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
            await response.prepare(request)

            queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
            self._active_stream_queues.add(queue)

            turn_task = asyncio.create_task(self.agent.submit_user_message(message))

            try:
                while not turn_task.done() or not queue.empty():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.2)
                        payload = f"data: {json.dumps(event)}\n\n"
                        await response.write(payload.encode("utf-8"))
                        if event.get("type") in ("turn_complete", "done"):
                            break
                    except asyncio.TimeoutError:
                        continue
            finally:
                self._active_stream_queues.discard(queue)
                await response.write_eof()

            return response

        # Asynchronous submission: queue task immediately and return status
        asyncio.create_task(self.agent.submit_user_message(message))
        return web.json_response({"status": "queued", "message": message})

    async def handle_api_command(self, request: web.Request) -> web.Response:
        assert self.storage is not None and self.view is not None
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        cmd = data.get("command", "").strip()

        if cmd == "/view":
            return web.json_response({"output": self.view.render()})

        elif cmd == "/stats":
            output = (
                f"Messages: {len(self.storage.messages):,}\n"
                f"Tree nodes: {len(self.storage.tree):,}\n"
                f"View size: {self.view.compute_size():,} / {self.view.budget:,} bytes "
                f"({(self.view.compute_size() / self.view.budget) * 100:.1f}%)\n"
                f"View settled: {self.view.is_settled()}"
            )
            return web.json_response({"output": output})

        elif cmd.startswith("/date"):
            parts = cmd.split()
            if len(parts) >= 2 and parts[1].isdigit():
                idx = int(parts[1])
                msg = self.storage.get_message(idx)
                if msg:
                    return web.json_response({"output": f"Message {idx}: {msg.date}"})
                return web.json_response({"output": f"Message {idx} not found."})
            return web.json_response({"output": "Usage: /date <id>"})

        elif cmd.startswith("/note"):
            text = cmd[5:].strip()
            if text:
                msg = self.storage.append_message("note", text)
                self.view.on_new_message(msg.i)
                if self.compactor:
                    self.compactor.pump()
                return web.json_response({"output": f"Logged note #{msg.i}: {text}"})
            return web.json_response({"output": "Usage: /note <text>"})

        elif cmd == "/rebuild":
            self.view.rebuild()
            return web.json_response({"output": f"View rebuilt. Size: {self.view.compute_size():,} bytes."})

        return web.json_response({"output": f"Unknown command: {cmd}"})

    async def handle_api_zoom(self, request: web.Request) -> web.Response:
        assert self.storage is not None
        try:
            data = await request.json()
            id_ = int(data.get("id"))
            n = int(data.get("n"))
        except Exception:
            return web.json_response({"error": "Invalid id or n"}, status=400)

        l, i = node_coords(id_, n)

        if n == 1:
            msg = self.storage.get_message(i)
            if msg:
                out = f"Verbatim Message {id_} ({msg.kind}):\n{msg.text}"
            else:
                out = f"Message {id_} not found."
            return web.json_response({"output": out})

        # Higher level zoom (§7.1)
        child_a = self.storage.get_node(l - 1, 2 * i)
        child_b = self.storage.get_node(l - 1, 2 * i + 1)

        half = n // 2
        a_text = child_a.text if child_a else "(not summarized yet: zoom it)"
        b_text = child_b.text if child_b else "(not summarized yet: zoom it)"

        out = (
            f"Zoomed line {id_}+{n} into span {half}:\n\n"
            f"{id_}+{half}|{a_text}\n"
            f"{id_ + half}+{half}|{b_text}"
        )
        return web.json_response({"output": out})

    def shutdown(self) -> None:
        if self.compactor:
            self.compactor.stop()
        if self.storage:
            self.storage.close()


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def run_web_app(
    host: str = "0.0.0.0",
    port: int = 8765,
    chat_dir: Optional[Path] = None,
    provider_name: str = "agy",
    model: Optional[str] = None,
    compactor_model: Optional[str] = None,
) -> None:
    target_dir = Path(chat_dir or (Path.home() / ".optchat")).resolve()
    server = OptChatWebServer(
        chat_dir=target_dir,
        provider_name=provider_name,
        model=model,
        compactor_model=compactor_model,
    )

    async def start():
        await server.init_engine()
        lan_ip = get_lan_ip()
        print("\n" + "=" * 55)
        print("  OptChat Web Interface Running")
        print("=" * 55)
        print(f"  • Local URL:   http://localhost:{port}")
        print(f"  • Network URL: http://{lan_ip}:{port}  (Open on Phone)")
        print(f"  • Chat Dir:    {target_dir}")
        print("=" * 55 + "\n")

        runner = web.AppRunner(server.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            server.shutdown()
            await runner.cleanup()

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        print("\nOptChat web server stopped.")
