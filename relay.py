#!/usr/bin/env python3
"""
relay.py - Muse Team Chat 自動中繼與輪詢服務
- 使用 SQLite 中央訊息庫 (messages 表)
- 使用 data/watermark.json 防止重複派送
- 支援 LLM (OpenAI 相容) 或內建角色規則 demo 模式
- 僅使用 Python 標準庫
"""

import sys
import os
import json
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# 基本成員定義
ROLES_CONFIG = {
    "boss": {
        "name": "老闆",
        "title": "最終拍板、旁觀全程",
        "color": "#D4AF37",
        "align": "center",
        "avatar": "🐱",
        "badge": "老闆指令",
    },
    "supervisor": {
        "name": "項目主管（pj主管）",
        "title": "統籌、整理、向老闆匯報",
        "color": "#008B8B",
        "align": "left",
        "avatar": "📋",
        "badge": "主管統籌",
    },
    "designer": {
        "name": "UIUX 設計師",
        "title": "設計資產、skill、品牌視覺，UIUX＋3D",
        "color": "#E25822",
        "align": "right",
        "avatar": "🎨",
        "badge": "視覺/UIUX",
    },
    "tin": {
        "name": "tin 軟件工程師",
        "title": "搞清需求、拆任務、派免費工具、再驗收",
        "color": "#4682B4",
        "align": "left",
        "avatar": "💻",
        "badge": "工程/派工",
    },
    "cin": {
        "name": "cin 驗收師",
        "title": "軟件安全性＋功能驗收、真任務探針",
        "color": "#8A2BE2",
        "align": "right",
        "avatar": "🔍",
        "badge": "探針驗收",
    },
    "money": {
        "name": "Money 額度管理",
        "title": "管 agy、Kilo、OpenCode 等額度與排程",
        "color": "#2E8B57",
        "align": "left",
        "avatar": "💰",
        "badge": "額度排程",
    },
}

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "data/chat.db")
DEFAULT_WATERMARK_PATH = os.environ.get("WATERMARK_PATH", "data/watermark.json")
DEFAULT_POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))


def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """獲取資料庫連接並確保結構完整"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                speaker TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'chat'
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id);")
    return conn


def load_watermark(watermark_path: str = DEFAULT_WATERMARK_PATH) -> int:
    """讀取 watermark，若檔案不存在或損壞則返回 0"""
    if not os.path.exists(watermark_path):
        return 0
    try:
        with open(watermark_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return int(data.get("last_id", 0))
    except Exception as e:
        sys.stderr.write(f"[WARN] 讀取 watermark 失敗 ({e})，重設為 0\n")
        return 0


def save_watermark(last_id: int, watermark_path: str = DEFAULT_WATERMARK_PATH) -> None:
    """原子化寫入 watermark"""
    os.makedirs(os.path.dirname(os.path.abspath(watermark_path)), exist_ok=True)
    temp_path = f"{watermark_path}.tmp.{os.getpid()}"
    data = {
        "last_id": last_id,
        "updated_at": datetime.now().isoformat()
    }
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, watermark_path)


def load_role_prompt(role: str) -> str:
    """載入 roles/{role}.md 的說明，若找不到則使用預設說明"""
    role_file = Path("roles") / f"{role}.md"
    if role_file.exists():
        try:
            return role_file.read_text(encoding="utf-8")
        except Exception:
            pass
    cfg = ROLES_CONFIG.get(role, {})
    return f"{cfg.get('name', role)}: {cfg.get('title', '')}"


def generate_llm_reply(role: str, message: dict, recent_history: list) -> str:
    """透過 OpenAI 相容端點請求 LLM 回覆（若有配置金鑰）"""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("MODEL", "gpt-4o-mini").strip()

    if not api_key:
        return ""

    role_desc = load_role_prompt(role)
    system_prompt = (
        f"你是團隊中的成員：{ROLES_CONFIG.get(role, {}).get('name', role)}。\n"
        f"你的職責定義如下：\n{role_desc}\n\n"
        "請根據最新訊息與歷史對話，以符合你角色立場的角度進行簡潔繁體中文回覆（2至3句話以內）。"
        "如果你認為此訊息不需要你回覆或與你職責無關，請僅回傳「SILENT」。"
    )

    history_lines = [f"{m['speaker']}: {m['body']}" for m in recent_history[-6:]]
    history_text = "\n".join(history_lines)
    user_prompt = f"最新對話記錄：\n{history_text}\n\n請以 {role} 身份回覆："

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 150
    }

    try:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            if content == "SILENT":
                return ""
            return content
    except Exception as e:
        sys.stderr.write(f"[WARN] LLM API 請求失敗 ({role}): {e}\n")
        return ""


def generate_demo_reply(role: str, incoming_msg: dict) -> str:
    """當無 LLM 金鑰時，根據角色定義與輸入訊息產生規則性 demo 回覆"""
    speaker = incoming_msg.get("speaker", "")
    body = incoming_msg.get("body", "")

    # 老闆發指令時，各成員根據職責依序回報
    if speaker == "boss":
        demo_map = {
            "supervisor": f"收到老闆指示！我已記錄並正在統籌全盤排期，將任務梳理後派發給各成員，會定時彙整進度匯報。",
            "tin": f"收到需求。我不會直接硬寫代碼，正將核心流程拆解為獨立模組任務，準備派給免費代碼工具並規劃接口規格。",
            "cin": f"收到。針對此需求，我已開始規劃真實任務探針與安全邊界測試案例，絕不以表層 PONG 敷衍，測試通過才驗收。",
            "money": f"額度監控中：當前各 AI 平臺免費配額充足，已排入最優先隊列，若有卡死將自動降級切換備用模型。",
            "designer": f"視覺與 UIUX 部分已在對齊設計資產與互動風格，保證各裝置無水平溢出且符合品牌色調。",
        }
        return demo_map.get(role, "")

    # 成員互相交流：若特定角色被提到（例如 @tin, @cin, 或提及關鍵字）
    body_lower = body.lower()
    if role == "tin" and ("tin" in body_lower or "工程" in body or "代碼" in body or "架構" in body):
        return "tin 收到：模組架構已理清，任務切片已完成，準備透過代碼生成工具產出第一版。"
    if role == "cin" and ("cin" in body_lower or "驗收" in body or "安全" in body or "測試" in body or "探針" in body):
        return "cin 收到：正以端到端真任務探針執行掃描，嚴查是否有硬編碼敏感資訊與邏輯漏洞。"
    if role == "money" and ("money" in body_lower or "額度" in body or "費用" in body or "工具" in body):
        return "money 收到：各模型使用量在正常水位內，持續監控並保留足額備用免費額度。"
    if role == "designer" and ("designer" in body_lower or "設計" in body or "視覺" in body or "ui" in body_lower):
        return "designer 收到：UIUX 佈局與視覺色標已完成調試，已確認行動端與桌面端排版適配良好。"
    if role == "supervisor" and ("主管" in body or "進度" in body or "匯報" in body or "supervisor" in body_lower):
        return "主管收到：已同步各成員反饋，正在整理當前里程碑摘要準備向老闆報告。"

    return ""


def dispatch_message(incoming_msg: dict, conn: sqlite3.Connection) -> list:
    """
    將新發生的訊息派送給其他所有成員，收集回覆並寫入資料庫
    回傳新增的回覆清單
    """
    speaker = incoming_msg["speaker"]
    msg_id = incoming_msg["id"]
    body = incoming_msg["body"]
    kind = incoming_msg["kind"]

    # 取得最近對話脈絡（避免傳遞過多無關數據）
    cur = conn.cursor()
    cur.execute("SELECT speaker, body FROM messages WHERE id <= ? ORDER BY id DESC LIMIT 10", (msg_id,))
    recent_history = [dict(r) for r in reversed(cur.fetchall())]

    has_llm = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    generated_replies = []

    # 派送給除了發言者之外的所有其他角色
    target_roles = [r for r in ROLES_CONFIG.keys() if r != speaker]

    for role in target_roles:
        reply_text = ""
        if has_llm:
            reply_text = generate_llm_reply(role, incoming_msg, recent_history)
        
        # 若無 LLM 或 LLM 無回覆（SILENT / 失敗），退回規則 Demo
        if not reply_text:
            reply_text = generate_demo_reply(role, incoming_msg)

        if reply_text:
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reply_kind = "command_reply" if kind in ("command", "boss") or speaker == "boss" else "chat"
            with conn:
                cur = conn.execute(
                    "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                    (role, reply_text, now_iso, reply_kind)
                )
                new_id = cur.lastrowid
                generated_replies.append({
                    "id": new_id,
                    "speaker": role,
                    "body": reply_text,
                    "created_at": now_iso,
                    "kind": reply_kind
                })

    return generated_replies


def run_once(db_path: str = DEFAULT_DB_PATH, watermark_path: str = DEFAULT_WATERMARK_PATH) -> list:
    """
    執行單次輪詢檢查：
    1. 讀取 watermark
    2. 查詢未處理訊息 (id > watermark)
    3. 依序派送處理，更新 watermark
    4. 回傳所有新產生的回覆訊息
    """
    conn = get_db_connection(db_path)
    last_id = load_watermark(watermark_path)
    
    cur = conn.cursor()
    cur.execute(
        "SELECT id, speaker, body, created_at, kind FROM messages WHERE id > ? ORDER BY id ASC",
        (last_id,)
    )
    unprocessed = [dict(r) for r in cur.fetchall()]
    
    if not unprocessed:
        conn.close()
        return []

    all_new_replies = []
    highest_processed_id = last_id

    for msg in unprocessed:
        # 如果這條訊息是其他成員自動產生的回覆且當前無特定艾特，我們記錄 watermark，但避免無限遞歸連鎖
        replies = dispatch_message(msg, conn)
        all_new_replies.extend(replies)
        if msg["id"] > highest_processed_id:
            highest_processed_id = msg["id"]

    # 注意：成員回覆產生了新的訊息 id，若將 watermark 移至 highest_processed_id，
    # 剛產生的回覆將會在下一輪作為 candidate；如果回覆本身沒有觸發任何其他角色，
    # 就不會產生新訊息。為了更乾淨地終結本輪事件，如果本輪由 boss 或用戶觸發，
    # 我們將 watermark 更新為所有已派送訊息的最大 id（包含剛產生的 replies），
    # 避免成員對成員的回覆產生無限反覆確認。
    if all_new_replies:
        max_reply_id = max(r["id"] for r in all_new_replies)
        highest_processed_id = max(highest_processed_id, max_reply_id)

    save_watermark(highest_processed_id, watermark_path)
    conn.close()
    return all_new_replies


def poll_loop(db_path: str = DEFAULT_DB_PATH, watermark_path: str = DEFAULT_WATERMARK_PATH, interval: float = DEFAULT_POLL_INTERVAL):
    """常駐輪詢主迴圈"""
    print(f"[RELAY] 啟動自動中繼輪詢服務 (間隔: {interval}s)")
    print(f"[RELAY] 資料庫路徑: {db_path}")
    print(f"[RELAY] 浮水印檔案: {watermark_path}")
    print(f"[RELAY] 模式: {'LLM 連接 (' + os.environ.get('MODEL', '') + ')' if os.environ.get('OPENAI_API_KEY') else '角色規則 Demo 模式'}")

    try:
        while True:
            replies = run_once(db_path, watermark_path)
            if replies:
                for r in replies:
                    print(f"[RELAY 回覆] #{r['id']} [{r['speaker']}] {r['body']}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[RELAY] 服務停止。")


if __name__ == "__main__":
    if "--once" in sys.argv:
        replies = run_once()
        print(f"[RELAY --once] 處理完畢，新增 {len(replies)} 條回覆。")
        for r in replies:
            print(f"  #{r['id']} [{r['speaker']}]: {r['body']}")
    elif "--reset-watermark" in sys.argv:
        save_watermark(0)
        print("[RELAY] 浮水印已重設為 0。")
    else:
        poll_loop()
