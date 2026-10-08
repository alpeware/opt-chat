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
from optchat.engine_client import EngineClient, ProxyCompactor, ProxyStorage, ProxyView
from optchat.providers import create_provider
from optchat.providers.agy_provider import AgyProvider
from optchat.providers.base import BaseLLMProvider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.tree import node_coords
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file

logger = logging.getLogger("optchat.web")

# Embedded, self-contained HTML/CSS/JS frontend (no external CDN required)
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content">
  <title>OptChat Web</title>
  <style>
    :root {
      --app-height: 100%;
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
      height: var(--app-height, 100%);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      overflow: hidden;
      width: 100%;
      margin: 0;
      padding: 0;
    }
    #app {
      display: flex;
      flex-direction: column;
      height: 100%;
      height: var(--app-height, 100%);
      max-width: 900px;
      margin: 0 auto;
      position: relative;
      overflow: hidden;
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
    .message-row.work { align-items: flex-start; }
    .work .bubble {
      background: #18112e;
      border: 1px solid #4c1d95;
      border-left: 4px solid var(--purple);
      color: #f1f5f9;
      border-bottom-left-radius: 4px;
    }
    .work .message-sender {
      color: #c084fc;
      font-weight: 600;
    }

    .subagent-card {
      font-size: 0.85rem;
      background: #18112e;
      border: 1px solid #4c1d95;
      border-left: 4px solid var(--purple);
      padding: 10px 14px;
      border-radius: 10px;
      width: 100%;
      max-width: 90%;
      margin: 6px 0;
      color: #cbd5e1;
    }
    .subagent-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-weight: 600;
      font-size: 0.88rem;
      color: #d8b4fe;
      margin-bottom: 6px;
    }
    .subagent-badge {
      background: rgba(168, 85, 247, 0.25);
      color: #c084fc;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .subagent-prompt {
      font-size: 0.82rem;
      color: #cbd5e1;
      margin-bottom: 6px;
      line-height: 1.4;
    }
    .subagent-tools {
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-top: 6px;
    }
    .subagent-tool-item {
      font-size: 0.78rem;
      font-family: monospace;
      background: #0d091a;
      border: 1px solid #2e1065;
      padding: 6px 10px;
      border-radius: 6px;
      color: #a78bfa;
    }
    .subagent-tool-item summary { cursor: pointer; outline: none; }
    .subagent-tool-item pre { margin-top: 4px; white-space: pre-wrap; font-size: 0.74rem; color: #94a3b8; }

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
    /* Markdown Tables */
    .md-table-wrap {
      width: 100%;
      overflow-x: auto;
      margin: 12px 0;
      border-radius: 8px;
      border: 1px solid var(--card-border);
      -webkit-overflow-scrolling: touch;
    }
    .bubble table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
      text-align: left;
      line-height: 1.4;
    }
    .bubble th, .bubble td {
      padding: 8px 12px;
      border-bottom: 1px solid var(--card-border);
      border-right: 1px solid var(--card-border);
    }
    .bubble th:last-child, .bubble td:last-child {
      border-right: none;
    }
    .bubble tr:last-child td {
      border-bottom: none;
    }
    .bubble th {
      background: rgba(255, 255, 255, 0.06);
      font-weight: 600;
      color: var(--accent);
      white-space: nowrap;
    }
    .bubble tbody tr:nth-child(even) {
      background: rgba(255, 255, 255, 0.02);
    }
    .bubble tbody tr:hover {
      background: rgba(255, 255, 255, 0.04);
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
      font-size: 16px; /* 16px prevents mobile browser auto-zoom on focus */
      border-radius: 12px;
      padding: 11px 14px;
      resize: none;
      min-height: 46px;
      height: 46px;
      max-height: 160px;
      outline: none;
      line-height: 1.45;
      font-family: inherit;
      overflow-y: auto;
    }
    #prompt-input:focus { border-color: var(--accent); }
    #send-btn {
      width: 46px;
      height: 46px;
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
      <div class="stats-pills" id="stats-bar">
        <span class="pill" id="pill-ws" title="Click to view/change workspace" onclick="openSessionModal()">📁 opt-chat</span>
        <span class="pill" id="pill-msgs" onclick="openStats()">Msgs: -</span>
        <span class="pill" id="pill-nodes" onclick="openStats()">Nodes: -</span>
        <span class="pill" id="pill-view" onclick="openStats()">View: -</span>
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
        <textarea id="prompt-input" rows="1" placeholder="Type a message or /command... (Ctrl+Enter to send)" onkeydown="handleKeyDown(event)" oninput="autoGrow(this)"></textarea>
        <button type="submit" id="send-btn" title="Send (Ctrl+Enter)">
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

    function setAppHeight() {
      const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
      document.documentElement.style.setProperty('--app-height', `${h}px`);
      const app = document.getElementById('app');
      if (app) app.style.height = `${h}px`;
    }

    // Load initial state, history & connect persistent event stream
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
      } else if (event.type === 'subagent_spawn') {
        let card = document.getElementById('active-subagent-card');
        const role = (event.extra && event.extra.role) || 'Subagent';
        const typeName = (event.extra && event.extra.typeName) || 'research';
        const prompt = (event.extra && event.extra.prompt) || event.content;
        if (!card) {
          card = document.createElement('div');
          card.className = 'subagent-card';
          card.id = 'active-subagent-card';
          card.innerHTML = `
            <div class="subagent-header">
              <span>🚀 Subagent: <b>${role}</b></span>
              <span class="subagent-badge">${typeName}</span>
            </div>
            <div class="subagent-prompt">${formatMarkdown(prompt)}</div>
            <div class="subagent-tools" id="subagent-tools-container"></div>
          `;
          const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
          document.getElementById('chat-stream').insertBefore(card, target);
        } else {
          const badge = card.querySelector('.subagent-badge');
          if (badge) badge.textContent = typeName;
        }
        scrollToBottom();
      } else if (event.type === 'subagent_tool') {
        let toolsContainer = document.getElementById('subagent-tools-container');
        if (!toolsContainer) {
          const toolEl = document.createElement('details');
          toolEl.className = 'tool-box';
          toolEl.style.borderLeftColor = 'var(--purple)';
          toolEl.innerHTML = `<summary>⚡ ${event.content}</summary><pre>${JSON.stringify(event.extra || {}, null, 2)}</pre>`;
          const target = currentAssistantBubble ? currentAssistantBubble.parentElement : document.getElementById('chat-stream');
          document.getElementById('chat-stream').insertBefore(toolEl, target);
        } else {
          const toolItem = document.createElement('details');
          toolItem.className = 'subagent-tool-item';
          const toolName = (event.extra && event.extra.tool) || 'tool';
          const argsPreview = JSON.stringify((event.extra && event.extra.args) || {});
          toolItem.innerHTML = `<summary>⚡ ${toolName}: ${argsPreview.slice(0, 90)}${argsPreview.length > 90 ? '...' : ''}</summary><pre>${JSON.stringify(event.extra || {}, null, 2)}</pre>`;
          toolsContainer.appendChild(toolItem);
        }
        scrollToBottom();
      } else if (event.type === 'subagent_complete') {
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
        appendMessage('work', `### 📋 Subagent Report: ${role}\n\n${repText}`, new Date().toISOString(), false);
        scrollToBottom();
        fetchState();
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
      const newWs = prompt("Active Workspace:\\n" + currentWorkspace + "\\n\\nEnter new workspace path to switch (e.g. /home/simonpure):", currentWorkspace);
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

    function appendMessage(kind, text, dateStr, isStreaming = false) {
      const stream = document.getElementById('chat-stream');
      const row = document.createElement('div');
      row.className = `message-row ${kind}`;
      
      const sender = document.createElement('span');
      sender.className = 'message-sender';
      sender.textContent = kind === 'user' ? 'You' : (kind === 'talk' ? 'OptChat' : (kind === 'work' ? 'SUBAGENT' : kind.toUpperCase()));
      
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

      // Tables, paragraphs, headers, rules, lists, quotes
      const lines = escaped.split('\\n');
      const output = [];
      let i = 0;

      function isTableSep(str) {
        const s = str.trim();
        if (!s.includes('|') || !s.includes('-')) return false;
        return /^[|]?([\\s]*:?-+:?[\\s]*[|])*[\\s]*:?-+:?[\\s]*[|]?$/.test(s);
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

        if (/^---+$/.test(trimmed) || /^\\*\\*\\*+$/.test(trimmed) || /^___+$/.test(trimmed)) {
          output.push('<hr>');
          i++;
          continue;
        }
        if (/^###\\s+(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^###\\s+(.+)$/, '<h3 class="md-h3">$1</h3>'));
          i++;
          continue;
        }
        if (/^##\\s+(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^##\\s+(.+)$/, '<h2 class="md-h2">$1</h2>'));
          i++;
          continue;
        }
        if (/^#\\s+(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^#\\s+(.+)$/, '<h1 class="md-h1">$1</h1>'));
          i++;
          continue;
        }
        if (/^####\\s+(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^####\\s+(.+)$/, '<h4 class="md-h4">$1</h4>'));
          i++;
          continue;
        }
        if (/^[\\*\\-]\\s+(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^[\\*\\-]\\s+(.+)$/, '<li class="md-li">$1</li>'));
          i++;
          continue;
        }
        if (/^\\d+\\.\\s+(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^(\\d+\\.)\\s+(.+)$/, '<li class="md-li-num"><b>$1</b> $2</li>'));
          i++;
          continue;
        }
        if (/^>\\s*(.+)$/.test(trimmed)) {
          output.push(trimmed.replace(/^>\\s*(.+)$/, '<blockquote>$1</blockquote>'));
          i++;
          continue;
        }
        output.push(`<p>${line}</p>`);
        i++;
      }
      return output.join('');
    }

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
        workspace: Optional[Path] = None,
        conversation_id: Optional[str] = None,
    ):
        self.chat_dir = Path(chat_dir).resolve()
        self.provider_name = provider_name
        self.model = model
        self.compactor_model = compactor_model
        self.timeout = timeout

        default_ws = Path("/home/simonpure/src/alpeware/opt-chat")
        self.workspace = (Path(workspace).resolve() if workspace else (default_ws if default_ws.exists() else Path.cwd())).resolve()
        self.conversation_id: Optional[str] = conversation_id

        self.engine_client: Optional[EngineClient] = None
        self.is_daemon_connected: bool = False
        self.storage: Optional[Any] = None
        self.view: Optional[Any] = None
        self.compactor: Optional[Any] = None
        self.agent: Optional[TurnAgent] = None
        self.main_provider: Optional[BaseLLMProvider] = None
        self.app = web.Application()

        self._active_stream_queues: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._daemon_event_task: Optional[asyncio.Task] = None

    async def init_engine(self) -> None:
        client = EngineClient(socket_path=self.chat_dir / "engine.sock")
        if await client.is_daemon_alive_async(timeout=0.5):
            logger.info("OptChat Web connected to background daemon via %s", client.socket_path)
            self.engine_client = client
            self.is_daemon_connected = True
            self.storage = ProxyStorage(client, self.chat_dir)
            self.view = ProxyView(client)
            self.compactor = ProxyCompactor(client)
            self._daemon_event_task = asyncio.create_task(self._listen_to_daemon_events())
        else:
            logger.info("OptChat Daemon not running. Using embedded Storage & Compactor.")
            self.engine_client = None
            self.is_daemon_connected = False
            self.storage = Storage(self.chat_dir)
            self.storage.open()

            self.view = LiveView(self.storage)
            self.view.rebuild()

            comp_provider = (
                create_provider(self.provider_name, model=self.compactor_model, timeout=self.timeout)
                if self.compactor_model
                else None
            )
            self.compactor = Compactor(self.storage, self.view, comp_provider)
            self.compactor.start()

        self.main_provider = create_provider(
            self.provider_name,
            model=self.model,
            timeout=self.timeout,
            workspace=str(self.workspace),
            conversation_id=self.conversation_id,
        )

        tool_registry = ToolRegistry()

        def ui_listener(event_type: str, content: str) -> None:
            # Sync conversation_id if AgyProvider captured one
            if isinstance(self.main_provider, AgyProvider) and self.main_provider.conversation_id:
                self.conversation_id = self.main_provider.conversation_id

            # Broadcast to active SSE listeners
            event_obj = {"type": event_type, "content": content}
            for q in list(self._active_stream_queues):
                try:
                    q.put_nowait(event_obj)
                except Exception:
                    pass

        agents_md = self.workspace / "AGENTS.md"
        if not agents_md.exists():
            agents_md = self.chat_dir.parent / "AGENTS.md"

        self.agent = TurnAgent(
            storage=self.storage,
            view=self.view,
            compactor=self.compactor,
            provider=self.main_provider,
            tool_registry=tool_registry,
            agents_md_path=agents_md if agents_md.exists() else None,
            git_auto_commit=False,
            ui_callback=ui_listener,
        )

        self._setup_routes()

    async def _listen_to_daemon_events(self) -> None:
        """Stream real-time daemon events (including agy subagent activities) to connected web clients."""
        while self.is_daemon_connected:
            try:
                assert self.engine_client is not None
                async for evt in self.engine_client.subscribe_events():
                    for q in list(self._active_stream_queues):
                        try:
                            q.put_nowait(evt)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Daemon event subscription retry: %s", e)
                await asyncio.sleep(2.0)

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/browse", self.handle_browse)
        self.app.router.add_get("/api/state", self.handle_api_state)
        self.app.router.add_get("/api/session", self.handle_api_session_get)
        self.app.router.add_post("/api/session", self.handle_api_session_post)
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
        return web.Response(
            text=HTML_PAGE,
            content_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    async def handle_browse(self, request: web.Request) -> web.Response:
        browse_file = self.chat_dir / "browse.html"
        if self.is_daemon_connected and self.engine_client:
            try:
                await self.engine_client.call_async("export_browse")
            except Exception:
                pass
        elif self.storage is not None and self.view is not None:
            if not browse_file.exists():
                export_html_to_file(self.storage, self.view, browse_file)
        if browse_file.exists():
            return web.FileResponse(browse_file)
        return web.Response(text="browse.html not available yet", status=404)

    async def handle_api_session_get(self, request: web.Request) -> web.Response:
        return web.json_response({
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
            "is_daemon_connected": self.is_daemon_connected,
            "provider": self.provider_name,
            "model": self.model,
        })

    async def handle_api_session_post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        if "workspace" in data:
            ws_path = Path(data["workspace"]).expanduser().resolve()
            if not ws_path.is_dir():
                return web.json_response({"error": f"Directory not found: {ws_path}"}, status=400)
            self.workspace = ws_path
            if isinstance(self.main_provider, AgyProvider):
                self.main_provider.workspace = str(self.workspace)
            agents_md = self.workspace / "AGENTS.md"
            if self.agent:
                self.agent.agents_md_path = agents_md if agents_md.exists() else None

        if "conversation_id" in data:
            conv_id = data["conversation_id"]
            self.conversation_id = conv_id if conv_id else None
            if isinstance(self.main_provider, AgyProvider):
                self.main_provider.conversation_id = self.conversation_id
                self.main_provider.last_conversation_id = self.conversation_id

        return web.json_response({
            "status": "ok",
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
        })

    async def handle_api_state(self, request: web.Request) -> web.Response:
        if self.is_daemon_connected and self.engine_client:
            st = await self.engine_client.get_state_async()
            return web.json_response({
                "messages_count": st.get("messages_count", 0),
                "tree_nodes_count": st.get("tree_nodes_count", 0),
                "view_size": st.get("view_size", 0),
                "view_budget": st.get("view_budget", 128000),
                "is_settled": st.get("is_settled", True),
                "daemon_connected": True,
                "workspace": str(self.workspace),
                "conversation_id": self.conversation_id,
            })

        assert self.storage is not None and self.view is not None
        return web.json_response({
            "messages_count": len(self.storage.messages),
            "tree_nodes_count": len(self.storage.tree),
            "view_size": self.view.compute_size(),
            "view_budget": self.view.budget,
            "is_settled": self.view.is_settled(),
            "daemon_connected": False,
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
        })

    async def handle_api_history(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        if self.is_daemon_connected and self.engine_client:
            res = await self.engine_client.call_async("get_history", limit=limit)
            return web.json_response(res.get("messages", []))

        assert self.storage is not None
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
            if m.kind in ("user", "talk", "note", "work")
        ]
        return web.json_response(result)

    async def handle_api_view(self, request: web.Request) -> web.Response:
        if self.is_daemon_connected and self.engine_client:
            res = await self.engine_client.call_async("get_view")
            return web.json_response({
                "size": res.get("size", 0),
                "budget": 128000,
                "lines": res.get("lines", []),
            })

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
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        cmd = data.get("command", "").strip()

        if cmd == "/view":
            if self.is_daemon_connected and self.engine_client:
                v = await self.engine_client.get_view_async()
                return web.json_response({"output": v})
            assert self.view is not None
            return web.json_response({"output": self.view.render()})

        elif cmd == "/stats":
            if self.is_daemon_connected and self.engine_client:
                st = await self.engine_client.get_state_async()
                output = (
                    f"Messages: {st.get('messages_count', 0):,}\n"
                    f"Tree nodes: {st.get('tree_nodes_count', 0):,}\n"
                    f"View size: {st.get('view_size', 0):,} / {st.get('view_budget', 128000):,} bytes "
                    f"({(st.get('view_size', 0) / max(1, st.get('view_budget', 128000))) * 100:.1f}%)\n"
                    f"View settled: {st.get('is_settled', True)}\n"
                    f"Engine Mode: Daemon IPC ({self.engine_client.socket_path})\n"
                    f"Workspace: {self.workspace}\n"
                    f"Conversation: {self.conversation_id or 'none (fresh)'}"
                )
                return web.json_response({"output": output})

            assert self.storage is not None and self.view is not None
            output = (
                f"Messages: {len(self.storage.messages):,}\n"
                f"Tree nodes: {len(self.storage.tree):,}\n"
                f"View size: {self.view.compute_size():,} / {self.view.budget:,} bytes "
                f"({(self.view.compute_size() / self.view.budget) * 100:.1f}%)\n"
                f"View settled: {self.view.is_settled()}\n"
                f"Engine Mode: Embedded Storage\n"
                f"Workspace: {self.workspace}\n"
                f"Conversation: {self.conversation_id or 'none (fresh)'}"
            )
            return web.json_response({"output": output})

        elif cmd.startswith("/date"):
            parts = cmd.split()
            if len(parts) >= 2 and parts[1].isdigit():
                idx = int(parts[1])
                if self.is_daemon_connected and self.engine_client:
                    out = await self.engine_client.date_async(idx)
                    return web.json_response({"output": out})
                assert self.storage is not None
                msg = self.storage.get_message(idx)
                if msg:
                    return web.json_response({"output": f"Message {idx}: {msg.date}"})
                return web.json_response({"output": f"Message {idx} not found."})
            return web.json_response({"output": "Usage: /date <id>"})

        elif cmd.startswith("/note"):
            text = cmd[5:].strip()
            if text:
                if self.is_daemon_connected and self.engine_client:
                    res = await self.engine_client.append_message_async("note", text)
                    m = res.get("message", {})
                    return web.json_response({"output": f"Logged note #{m.get('i')}: {text}"})
                assert self.storage is not None and self.view is not None
                msg = self.storage.append_message("note", text)
                self.view.on_new_message(msg.i)
                if self.compactor:
                    self.compactor.pump()
                return web.json_response({"output": f"Logged note #{msg.i}: {text}"})
            return web.json_response({"output": "Usage: /note <text>"})

        elif cmd.startswith("/workspace"):
            parts = cmd.split(maxsplit=1)
            if len(parts) == 2:
                new_path = Path(parts[1].strip()).expanduser().resolve()
                if new_path.is_dir():
                    self.workspace = new_path
                    if isinstance(self.main_provider, AgyProvider):
                        self.main_provider.workspace = str(self.workspace)
                    agents_md = self.workspace / "AGENTS.md"
                    if self.agent:
                        self.agent.agents_md_path = agents_md if agents_md.exists() else None
                    return web.json_response({"output": f"Workspace switched to: {self.workspace}"})
                return web.json_response({"output": f"Directory not found: {new_path}"})
            return web.json_response({"output": f"Current workspace: {self.workspace}"})

        elif cmd == "/rebuild":
            if self.is_daemon_connected and self.engine_client:
                res = await self.engine_client.rebuild_async()
                return web.json_response({"output": f"View rebuilt. Size: {res.get('size', 0):,} bytes."})
            assert self.view is not None
            self.view.rebuild()
            return web.json_response({"output": f"View rebuilt. Size: {self.view.compute_size():,} bytes."})

        return web.json_response({"output": f"Unknown command: {cmd}"})

    async def handle_api_zoom(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            id_ = int(data.get("id"))
            n = int(data.get("n"))
        except Exception:
            return web.json_response({"error": "Invalid id or n"}, status=400)

        if self.is_daemon_connected and self.engine_client:
            out = await self.engine_client.zoom_async(id_, n)
            return web.json_response({"output": out})

        assert self.storage is not None
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
        if self._daemon_event_task and not self._daemon_event_task.done():
            self._daemon_event_task.cancel()
        if not self.is_daemon_connected:
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
    workspace: Optional[Path] = None,
    conversation_id: Optional[str] = None,
) -> None:
    target_dir = Path(chat_dir or (Path.home() / ".optchat")).resolve()
    server = OptChatWebServer(
        chat_dir=target_dir,
        provider_name=provider_name,
        model=model,
        compactor_model=compactor_model,
        workspace=workspace,
        conversation_id=conversation_id,
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
