"""HTML Visualizer for OptChat memory (§10).

Exports current view, ROOT log, and binary tree levels into a self-contained
interactive HTML page with range, time span, and size for every entry.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List

from optchat.constants import VIEW
from optchat.storage import Storage
from optchat.tree import node_covers
from optchat.view import LiveView


def generate_html_report(storage: Storage, view: LiveView) -> str:
    """Generate interactive self-contained HTML page representing entire chat memory."""
    total_messages = len(storage.messages)
    view_bytes = view.compute_size()
    view_pct = round((view_bytes / VIEW) * 100, 1)

    # Group tree nodes by level
    levels: Dict[int, List[Dict[str, Any]]] = {}
    for (l, i), node in sorted(storage.tree.items(), key=lambda item: (item[0][0], item[0][1])):
        start, end = node_covers(l, i)
        span_str = f"[{start}, {end})"
        if l not in levels:
            levels[l] = []
        levels[l].append({
            "l": l,
            "i": i,
            "id": start,
            "n": end - start,
            "span": span_str,
            "text": node.text,
            "size": node.size,
        })

    # Prepare view items
    view_items = []
    for part in view.parts:
        node = storage.get_node(part.l, part.i)
        text = node.text if node else "(not summarized yet)"
        view_items.append({
            "l": part.l,
            "i": part.i,
            "id": part.id,
            "n": part.n,
            "text": text,
            "size": part.get_text_bytes(storage),
        })

    # Prepare root messages
    messages_data = []
    for msg in storage.messages:
        messages_data.append({
            "i": msg.i,
            "kind": msg.kind,
            "text": msg.text,
            "size": msg.size,
            "date": msg.date,
        })

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OptChat Memory Browser</title>
<style>
  :root {{
    --bg: #0f141c;
    --card: #18202c;
    --border: #283548;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --primary: #38bdf8;
    --success: #4ade80;
    --warn: #fbbf24;
    --purple: #c084fc;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    line-height: 1.5;
    padding: 24px;
  }}
  header {{
    border-bottom: 1px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 24px;
  }}
  h1 {{ font-size: 24px; font-weight: 700; color: #fff; }}
  .subtitle {{ color: var(--muted); font-size: 14px; margin-top: 4px; }}
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
  }}
  .stat-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }}
  .stat-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .stat-val {{ font-size: 24px; font-weight: 700; color: var(--primary); margin-top: 4px; }}
  .tabs {{
    display: flex;
    gap: 8px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 20px;
  }}
  .tab-btn {{
    background: none;
    border: none;
    color: var(--muted);
    font-size: 14px;
    font-weight: 600;
    padding: 10px 16px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
  }}
  .tab-btn.active {{
    color: var(--primary);
    border-bottom-color: var(--primary);
  }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
  }}
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    font-family: var(--mono);
  }}
  .badge-user {{ background: #1e3a5f; color: #60a5fa; }}
  .badge-talk {{ background: #064e3b; color: #34d399; }}
  .badge-tool {{ background: #4c1d95; color: #c084fc; }}
  .badge-echo {{ background: #78350f; color: #fde047; }}
  .badge-note {{ background: #374151; color: #9ca3af; }}
  .line-meta {{
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 4px;
    display: flex;
    justify-content: space-between;
  }}
  .content-mono {{
    font-family: var(--mono);
    font-size: 13px;
    white-space: pre-wrap;
    word-break: break-all;
    background: #0b0f17;
    padding: 10px;
    border-radius: 6px;
    border: 1px solid #1a2230;
  }}
  .level-title {{
    font-size: 16px;
    font-weight: 600;
    color: var(--warn);
    margin: 20px 0 10px 0;
  }}
  input[type="text"] {{
    width: 100%;
    max-width: 400px;
    padding: 8px 12px;
    background: #0b0f17;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: #fff;
    margin-bottom: 16px;
  }}
</style>
</head>
<body>

<header>
  <h1>OptChat Memory Browser</h1>
  <div class="subtitle">Complete inspectable log, binary summary tree, and live view</div>
</header>

<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-label">Total Messages (ROOT)</div>
    <div class="stat-val">{total_messages:,}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Tree Nodes</div>
    <div class="stat-val">{len(storage.tree):,}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Live View Size</div>
    <div class="stat-val">{view_bytes:,} B <span style="font-size:14px;color:var(--muted)">({view_pct}% of budget)</span></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Tree Levels</div>
    <div class="stat-val">{len(levels)}</div>
  </div>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="showTab('view')">Live View ({len(view.parts)} tiles)</button>
  <button class="tab-btn" onclick="showTab('tree')">Binary Tree ({len(storage.tree)} nodes)</button>
  <button class="tab-btn" onclick="showTab('root')">ROOT Messages ({total_messages})</button>
</div>

<!-- Tab: Live View -->
<div id="tab-view" class="tab-panel active">
  <p style="color:var(--muted);font-size:14px;margin-bottom:16px;">
    The live view is the continuous memory prefix presented to the model at each turn.
  </p>
  <div>
"""
    for item in view_items:
        html_content += f"""
    <div class="card">
      <div class="line-meta">
        <span><strong>{item['id']}+{item['n']}</strong> (level {item['l']}, idx {item['i']})</span>
        <span>{item['size']} bytes</span>
      </div>
      <div class="content-mono">{html.escape(item['text'])}</div>
    </div>
"""

    html_content += """
  </div>
</div>

<!-- Tab: Binary Tree -->
<div id="tab-tree" class="tab-panel">
"""
    for l in sorted(levels.keys()):
        node_count = len(levels[l])
        span_example = 1 << l
        html_content += f"""
  <div class="level-title">Level {l} &mdash; Covers {span_example} messages per node ({node_count} nodes)</div>
  <div style="display:grid;gap:12px;">
"""
        for item in levels[l]:
            html_content += f"""
    <div class="card">
      <div class="line-meta">
        <span><strong>{item['id']}+{item['n']}</strong> &bull; messages {item['span']}</span>
        <span>{item['size']} bytes</span>
      </div>
      <div class="content-mono">{html.escape(item['text'])}</div>
    </div>
"""
        html_content += "  </div>\n"

    html_content += """
</div>

<!-- Tab: ROOT Messages -->
<div id="tab-root" class="tab-panel">
  <input type="text" id="root-filter" placeholder="Filter messages by text..." onkeyup="filterRoot()">
  <div id="root-list">
"""
    for msg in messages_data:
        kind_class = f"badge-{msg['kind']}"
        html_content += f"""
    <div class="card root-msg-card">
      <div class="line-meta">
        <div>
          <span style="font-weight:700;margin-right:8px;">#{msg['i']}</span>
          <span class="badge {kind_class}">{msg['kind']}</span>
        </div>
        <span>{msg['date']} &bull; {msg['size']} B</span>
      </div>
      <div class="content-mono">{html.escape(msg['text'])}</div>
    </div>
"""

    html_content += """
  </div>
</div>

<script>
function showTab(id) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + id).classList.add('active');
}
function filterRoot() {
  const query = document.getElementById('root-filter').value.toLowerCase();
  document.querySelectorAll('.root-msg-card').forEach(card => {
    const text = card.innerText.toLowerCase();
    card.style.display = text.includes(query) ? '' : 'none';
  });
}
</script>

</body>
</html>
"""
    return html_content


def export_html_to_file(storage: Storage, view: LiveView, target_file: Path) -> Path:
    target_path = Path(target_file).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    html_data = generate_html_report(storage, view)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(html_data)
    return target_path
