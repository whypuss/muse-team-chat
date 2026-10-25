#!/usr/bin/env python3
"""
server.py - Muse Team Chat Web UI 與 REST API 伺服器
- 純 Python 標準庫 (http.server + sqlite3)
- 內建背景自動中繼 (relay) 執行緒，亦可與獨立 relay.py 協同工作
- 支援手機與桌面響應式介面、六位成員獨立顏色與對話框佈局
"""

import sys
import os
import json
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime

# 引入 relay 核心邏輯
import relay

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "data/chat.db")
WATERMARK_PATH = os.environ.get("WATERMARK_PATH", "data/watermark.json")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-HK">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Muse Team Chat — 團隊協作群聊</title>
<style>
  :root {
    --bg-main: #0f172a;
    --bg-panel: #1e293b;
    --bg-card: #273548;
    --text-primary: #f8fafc;
    --text-muted: #94a3b8;
    --border-color: #334155;
    
    /* 成員主題色 */
    --color-boss: #d4af37;
    --color-supervisor: #008b8b;
    --color-designer: #e25822;
    --color-tin: #4682b4;
    --color-cin: #8a2be2;
    --color-money: #2e8b57;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg-main);
    color: var(--text-primary);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    overflow-x: hidden;
  }

  /* 頂部導航欄 */
  header {
    background: rgba(30, 41, 59, 0.95);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border-color);
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .header-left { display: flex; align-items: center; gap: 12px; }
  .logo { font-size: 20px; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 6px; }
  .logo span { color: var(--color-boss); }
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(34, 197, 94, 0.15);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.3);
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #22c55e;
    box-shadow: 0 0 8px #22c55e; animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.85); }
  }

  /* 成員名錄膠囊列 */
  .members-bar {
    display: flex;
    gap: 8px;
    padding: 8px 16px;
    background: #172233;
    overflow-x: auto;
    white-space: nowrap;
    border-bottom: 1px solid var(--border-color);
    scrollbar-width: thin;
  }
  .member-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 500;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    flex-shrink: 0;
  }
  .chip-dot { width: 8px; height: 8px; border-radius: 50%; }

  /* 訊息聊天區域 */
  #chat-container {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 900px;
    width: 100%;
    margin: 0 auto;
  }

  .message-row {
    display: flex;
    width: 100%;
    animation: fadeIn 0.25s ease-out;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .message-row.align-left { justify-content: flex-start; }
  .message-row.align-right { justify-content: flex-end; }
  .message-row.align-center { justify-content: center; }

  /* 對話框卡片 */
  .bubble {
    max-width: 82%;
    border-radius: 14px;
    padding: 10px 14px;
    position: relative;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    border: 1px solid transparent;
  }

  /* 老闆特殊置中卡片 */
  .message-row.align-center .bubble {
    max-width: 92%;
    width: 100%;
    background: linear-gradient(135deg, rgba(212, 175, 55, 0.15) 0%, rgba(30, 41, 59, 0.95) 100%);
    border: 1px solid rgba(212, 175, 55, 0.45);
    box-shadow: 0 4px 20px rgba(212, 175, 55, 0.15);
    text-align: center;
  }
  .message-row.align-center .bubble-meta { justify-content: center; }
  .message-row.align-center .boss-badge {
    background: var(--color-boss);
    color: #000;
    font-size: 10px;
    font-weight: 800;
    padding: 2px 6px;
    border-radius: 4px;
    letter-spacing: 0.5px;
    margin-left: 6px;
  }

  /* 左邊卡片 */
  .message-row.align-left .bubble {
    background: #1e293b;
    border-top-left-radius: 4px;
  }
  /* 右邊卡片 */
  .message-row.align-right .bubble {
    background: #253347;
    border-top-right-radius: 4px;
  }

  .bubble-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 5px;
    font-size: 12px;
  }
  .author-name { font-weight: 700; display: inline-flex; align-items: center; gap: 4px; }
  .author-badge {
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 4px;
    opacity: 0.9;
  }
  .msg-time { color: var(--text-muted); font-size: 11px; margin-left: auto; }
  .message-row.align-center .msg-time { margin-left: 0; }

  .bubble-body {
    font-size: 14.5px;
    line-height: 1.55;
    word-break: break-word;
    color: #f1f5f9;
  }

  /* 底部輸入區域 */
  .input-panel {
    background: var(--bg-panel);
    border-top: 1px solid var(--border-color);
    padding: 12px 16px;
    position: sticky;
    bottom: 0;
    z-index: 40;
    max-width: 900px;
    width: 100%;
    margin: 0 auto;
  }

  /* 獨立「老闆專屬輸入」突出區塊 */
  .boss-action-box {
    background: linear-gradient(90deg, rgba(212, 175, 55, 0.12) 0%, rgba(30, 41, 59, 0.8) 100%);
    border: 1px solid rgba(212, 175, 55, 0.4);
    border-radius: 10px;
    padding: 8px 12px;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .boss-action-title {
    font-size: 12px;
    font-weight: 700;
    color: var(--color-boss);
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .boss-input {
    flex: 1;
    background: rgba(15, 23, 42, 0.8);
    border: 1px solid rgba(212, 175, 55, 0.4);
    border-radius: 6px;
    padding: 8px 12px;
    color: #fff;
    font-size: 13.5px;
    outline: none;
  }
  .boss-input:focus {
    border-color: var(--color-boss);
    box-shadow: 0 0 0 2px rgba(212, 175, 55, 0.2);
  }
  .boss-btn {
    background: linear-gradient(135deg, #d4af37, #b8860b);
    color: #000;
    font-weight: 700;
    font-size: 13px;
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    white-space: nowrap;
    transition: transform 0.1s, opacity 0.2s;
  }
  .boss-btn:hover { opacity: 0.9; }
  .boss-btn:active { transform: scale(0.97); }

  /* 一般成員發言列 */
  .general-form {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .role-select {
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    color: var(--text-primary);
    padding: 8px 10px;
    border-radius: 6px;
    font-size: 13px;
    outline: none;
    cursor: pointer;
  }
  .general-input {
    flex: 1;
    background: var(--bg-card);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    padding: 8px 12px;
    color: var(--text-primary);
    font-size: 13.5px;
    outline: none;
  }
  .general-input:focus { border-color: #38bdf8; }
  .send-btn {
    background: #0284c7;
    color: #fff;
    font-weight: 600;
    font-size: 13px;
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    white-space: nowrap;
  }
  .send-btn:hover { background: #0369a1; }

  /* 空狀態提示 */
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: 40px 16px;
    margin: auto;
  }
  .empty-icon { font-size: 36px; margin-bottom: 8px; }

  @media (max-width: 640px) {
    .boss-action-box { flex-direction: column; align-items: stretch; }
    .boss-btn { width: 100%; text-align: center; }
    .general-form { flex-wrap: wrap; }
    .role-select { width: 100%; }
    .general-input { width: calc(100% - 80px); }
    .bubble { max-width: 90%; }
  }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <div class="logo">⚡ Muse <span>Team Chat</span></div>
    <div class="status-badge"><span class="status-dot"></span> 自動中繼運作中 (5s)</div>
  </div>
  <div style="font-size: 12px; color: var(--text-muted);">
    無手動中轉 • 真正互相交流
  </div>
</header>

<!-- 成員色標名錄 -->
<div class="members-bar" id="members-bar">
  <!-- 動態載入或靜態直出 -->
</div>

<!-- 訊息流 -->
<main id="chat-container">
  <div class="empty-state" id="empty-state">
    <div class="empty-icon">💬</div>
    <div>中央訊息庫尚無訊息。請在下方以「老闆」或團隊成員身份發言。</div>
  </div>
</main>

<!-- 輸入區域 -->
<div class="input-panel">
  <!-- 獨立「老闆專屬輸入」突出區塊 -->
  <form id="boss-form" class="boss-action-box" onsubmit="sendBossMessage(event)">
    <div class="boss-action-title">👑 老闆指令發布</div>
    <input type="text" id="boss-input-text" class="boss-input" placeholder="發布最高指令，自動派送全體成員即刻響應..." autocomplete="off">
    <button type="submit" class="boss-btn">發布指令 ⚡</button>
  </form>

  <!-- 其他成員發言選項 -->
  <form id="general-form" class="general-form" onsubmit="sendGeneralMessage(event)">
    <select id="role-select" class="role-select" title="發言身分"></select>
    <input type="text" id="general-input-text" class="general-input" placeholder="以所選成員身分發言交流..." autocomplete="off">
    <button type="submit" class="send-btn">發送</button>
  </form>
</div>

<script>
  let lastMessageId = 0;
  let rolesData = {};

  // 獲取成員資料並渲染頂部列及下拉選單
  async function initMembers() {
    try {
      const res = await fetch('/api/members');
      rolesData = await res.json();
      renderMembersBar();
      populateRoleSelect();
    } catch(e) {
      console.error("載入成員失敗", e);
    }
  }

  function renderMembersBar() {
    const bar = document.getElementById('members-bar');
    bar.innerHTML = '';
    for (const [key, r] of Object.entries(rolesData)) {
      const chip = document.createElement('div');
      chip.className = 'member-chip';
      chip.style.borderColor = r.color;
      chip.innerHTML = `
        <span class="chip-dot" style="background:${r.color};"></span>
        <span style="color:${r.color}; font-weight:700;">${r.avatar} ${r.name}</span>
        <span style="color:var(--text-muted); font-size:11px;">(${r.badge})</span>
      `;
      bar.appendChild(chip);
    }
  }

  function populateRoleSelect() {
    const sel = document.getElementById('role-select');
    sel.innerHTML = '';
    for (const [key, r] of Object.entries(rolesData)) {
      if (key === 'boss') continue; // 老闆有專屬輸入框
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = `${r.avatar} ${r.name}`;
      sel.appendChild(opt);
    }
  }

  // 渲染單條訊息
  function appendMessageToDOM(msg) {
    const container = document.getElementById('chat-container');
    const emptyState = document.getElementById('empty-state');
    if (emptyState) emptyState.remove();

    const role = rolesData[msg.speaker] || {
      name: msg.speaker,
      color: '#94a3b8',
      align: 'left',
      avatar: '👤',
      badge: '成員'
    };

    const row = document.createElement('div');
    row.className = `message-row align-${role.align}`;
    row.id = `msg-${msg.id}`;

    const isBoss = (msg.speaker === 'boss');
    const borderStyle = isBoss ? `border-color: ${role.color};` : `border-left: 3px solid ${role.color};`;

    row.innerHTML = `
      <div class="bubble" style="${borderStyle}">
        <div class="bubble-meta">
          <span class="author-name" style="color: ${role.color};">
            ${role.avatar} ${role.name}
            <span class="author-badge" style="background:${role.color}22; color:${role.color}; border: 1px solid ${role.color}44;">
              ${role.badge}
            </span>
            ${isBoss ? '<span class="boss-badge">老闆指令</span>' : ''}
          </span>
          <span class="msg-time">${msg.created_at}</span>
        </div>
        <div class="bubble-body">${escapeHTML(msg.body)}</div>
      </div>
    `;

    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
  }

  function escapeHTML(str) {
    return (str || '').replace(/[&<>'"]/g, 
      tag => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        "'": '&#39;',
        '"': '&quot;'
      }[tag] || tag)
    );
  }

  // 輪詢載入新訊息
  async function fetchNewMessages() {
    try {
      const res = await fetch(`/api/messages?after=${lastMessageId}`);
      if (!res.ok) return;
      const messages = await res.json();
      if (messages && messages.length > 0) {
        messages.forEach(msg => {
          if (msg.id > lastMessageId) {
            appendMessageToDOM(msg);
            lastMessageId = msg.id;
          }
        });
      }
    } catch(e) {
      console.error("輪詢新訊息出錯", e);
    }
  }

  // 發送訊息
  async function sendMessage(speaker, body, kind='chat') {
    if (!body || !body.trim()) return;
    try {
      const res = await fetch('/api/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaker, body: body.trim(), kind })
      });
      if (res.ok) {
        // 發送後立即輪詢一次，及時渲染
        setTimeout(fetchNewMessages, 300);
      }
    } catch(e) {
      alert("發送訊息失敗: " + e);
    }
  }

  function sendBossMessage(e) {
    e.preventDefault();
    const input = document.getElementById('boss-input-text');
    const val = input.value;
    if (val.trim()) {
      sendMessage('boss', val, 'command');
      input.value = '';
    }
  }

  function sendGeneralMessage(e) {
    e.preventDefault();
    const sel = document.getElementById('role-select');
    const input = document.getElementById('general-input-text');
    const val = input.value;
    if (val.trim()) {
      sendMessage(sel.value, val, 'chat');
      input.value = '';
    }
  }

  // 初始化啟動
  initMembers().then(() => {
    fetchNewMessages();
    setInterval(fetchNewMessages, 2500);
  });
</script>

</body>
</html>
"""


class ChatRequestHandler(BaseHTTPRequestHandler):
    """處理 REST API 與靜態頁面請求"""

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_text):
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._send_html(HTML_PAGE)
            return

        if path == "/api/members":
            self._send_json(relay.ROLES_CONFIG)
            return

        if path == "/api/health":
            conn = relay.get_db_connection(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM messages")
            count = cur.fetchone()["total"]
            conn.close()
            watermark = relay.load_watermark(WATERMARK_PATH)
            self._send_json({
                "status": "healthy",
                "message_count": count,
                "watermark": watermark,
                "poll_interval": POLL_INTERVAL,
                "llm_enabled": bool(os.environ.get("OPENAI_API_KEY"))
            })
            return

        if path == "/api/messages":
            after_id = 0
            if "after" in query:
                try:
                    after_id = int(query["after"][0])
                except ValueError:
                    after_id = 0
            
            conn = relay.get_db_connection(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT id, speaker, body, created_at, kind FROM messages WHERE id > ? ORDER BY id ASC LIMIT 100",
                (after_id,)
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            self._send_json(rows)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/messages":
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self._send_json({"error": "Empty body"}, 400)
                return

            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            speaker = data.get("speaker", "boss")
            body = data.get("body", "").strip()
            kind = data.get("kind", "command" if speaker == "boss" else "chat")

            if not body:
                self._send_json({"error": "Message body cannot be empty"}, 400)
                return

            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = relay.get_db_connection(DB_PATH)
            with conn:
                cur = conn.execute(
                    "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                    (speaker, body, now_iso, kind)
                )
                new_id = cur.lastrowid
            conn.close()

            # 收到新訊息後，觸發單次中繼派送（若在同一進程內）
            try:
                relay.run_once(DB_PATH, WATERMARK_PATH)
            except Exception as e:
                sys.stderr.write(f"[WARN] 即時中繼派送異常: {e}\n")

            self._send_json({
                "id": new_id,
                "speaker": speaker,
                "body": body,
                "created_at": now_iso,
                "kind": kind
            }, 201)
            return

        if path == "/api/relay":
            replies = relay.run_once(DB_PATH, WATERMARK_PATH)
            self._send_json({"dispatched": len(replies), "replies": replies})
            return

        self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        # 覆寫預設 access log 以免洗螢幕
        if "--verbose" in sys.argv:
            super().log_message(format, *args)


def start_background_relay():
    """以守護執行緒常駐運行中繼輪詢"""
    def loop():
        while True:
            try:
                relay.run_once(DB_PATH, WATERMARK_PATH)
            except Exception as e:
                sys.stderr.write(f"[RELAY-THREAD ERROR] {e}\n")
            time.sleep(POLL_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="RelayPollThread")
    t.start()
    return t


def run_server(port: int = PORT):
    # 初始化資料庫
    conn = relay.get_db_connection(DB_PATH)
    conn.close()

    # 啟動自動中繼背景輪詢執行緒
    if "--no-relay" not in sys.argv:
        start_background_relay()
        print(f"[SERVER] 已啟動背景自動中繼輪詢執行緒 (間隔: {POLL_INTERVAL}s)")

    server_address = ("", port)
    httpd = HTTPServer(server_address, ChatRequestHandler)
    print(f"[SERVER] Muse Team Chat 伺服器已啟動: http://localhost:{port}")
    print(f"[SERVER] 資料庫: {DB_PATH}")
    print(f"[SERVER] 按 Ctrl+C 可停止伺服器")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] 伺服器已停止。")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
