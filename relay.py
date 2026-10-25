#!/usr/bin/env python3
"""
relay.py - Muse Team Chat 自動中繼與輪詢服務
- 使用 SQLite 中央訊息庫 (messages 表)
- 使用 data/watermark.json 防止重複派送
- 輕量零 Token 輪詢（純 Python 查 SQLite，有新訊息才喚醒 LLM）
- 訊息前置智能分流 (Pre-routing)，節省大量 Token
- 突發訊息自動合併 (Burst Merge)
- 支援 LLM (OpenAI 相容) 或內建角色規則 demo 模式
- 僅使用 Python 標準庫
"""

import sys
import os
import re
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

# 智能分流關鍵字定義
SILENCE_KEYWORDS = ["不用回覆", "不用回", "不用回复", "靜默", "静默"]
BROADCAST_KEYWORDS = ["全體", "全体", "全部", "所有人", "大家"]

ROLE_KEYWORDS = {
    "tin": {
        "words": ["tin"],
        "terms": ["工程", "代碼", "代码", "架構", "架构"],
    },
    "cin": {
        "words": ["cin"],
        "terms": ["驗收", "验收", "安全", "測試", "测试", "探針", "探针"],
    },
    "money": {
        "words": ["money"],
        "terms": ["額度", "额度", "費用", "费用", "工具"],
    },
    "designer": {
        "words": ["designer", "ui", "uiux"],
        "terms": ["設計", "设计", "視覺", "视觉"],
    },
    "supervisor": {
        "words": ["supervisor"],
        "terms": ["主管", "進度", "进度", "匯報", "汇报", "pj主管"],
    },
}

# 內建角色規則 demo 回覆範本
DEMO_REPLIES = {
    "supervisor": {
        "boss": "收到老闆指示！我已記錄並正在統籌全盤排期，將任務梳理後派發給各成員，會定時彙整進度匯報。",
        "general": "主管收到：已同步各成員反饋，正在整理當前里程碑摘要準備向老闆報告。"
    },
    "tin": {
        "boss": "收到需求。我不會直接硬寫代碼，正將核心流程拆解為獨立模組任務，準備派給免費代碼工具並規劃接口規格。",
        "general": "tin 收到：模組架構已理清，任務切片已完成，準備透過代碼生成工具產出第一版。"
    },
    "cin": {
        "boss": "收到。針對此需求，我已開始規劃真實任務探針與安全邊界測試案例，絕不以表層 PONG 敷衍，測試通過才驗收。",
        "general": "cin 收到：正以端到端真任務探針執行掃描，嚴查是否有硬編碼敏感資訊與邏輯漏洞。"
    },
    "money": {
        "boss": "額度監控中：當前各 AI 平臺免費配額充足，已排入最優先隊列，若有卡死將自動降級切換備用模型。",
        "general": "money 收到：各模型使用量在正常水位內，持續監控並保留足額備用免費額度。"
    },
    "designer": {
        "boss": "視覺與 UIUX 部分已在對齊設計資產與互動風格，保證各裝置無水平溢出且符合品牌色調。",
        "general": "designer 收到：UIUX 佈局與視覺色標已完成調試，已確認行動端與桌面端排版適配良好。"
    }
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


def _contains_word(text: str, word: str) -> bool:
    """檢查英文詞彙或代號是否獨立出現（前後非英數字底線），避免 continue 誤中 tin 或 fruit 誤中 ui"""
    pattern = rf"(?i)(?<![a-zA-Z0-9_]){re.escape(word)}(?![a-zA-Z0-9_])"
    return bool(re.search(pattern, text))


def match_role_keywords(role: str, body: str) -> bool:
    """判斷訊息內文是否包含該角色的關鍵字或 @提及"""
    cfg = ROLE_KEYWORDS.get(role)
    if not cfg:
        return False
    for term in cfg["terms"]:
        if term in body:
            return True
    for word in cfg["words"]:
        if _contains_word(body, word):
            return True
    return False


def get_target_roles(incoming_msg: dict) -> list:
    """
    分流規則（在調用 LLM 前先做過濾）：
    - 訊息含「不用回覆／不用回／靜默」→ 全部靜默，零回覆零 LLM 調用。
    - 訊息含「全體／全部／所有人／大家」→ 全員回覆。
    - 老闆訊息（非全體）：主管 supervisor 一定回覆統籌，其他角色只在被 @提及、被點名、或命中其職責關鍵字時才回覆。
    - 成員訊息：只回覆被 @提及／點名／命中關鍵字的角色；若無人命中，主管在含「進度／匯報／主管」時才回覆，否則零回覆。
    """
    routing_enabled = os.environ.get("ROUTING_ENABLED", "1").strip().lower() not in ("0", "false", "no")
    speaker = incoming_msg.get("speaker", "")
    body = incoming_msg.get("body", "")
    all_other_roles = [r for r in ROLES_CONFIG.keys() if r != speaker]

    if not routing_enabled:
        return all_other_roles

    # 1. 訊息含「不用回覆／不用回／靜默」→ 全部靜默，零回覆零 LLM 調用
    for kw in SILENCE_KEYWORDS:
        if kw in body:
            return []

    # 2. 訊息含「全體／全部／所有人／大家」→ 全員回覆
    for kw in BROADCAST_KEYWORDS:
        if kw in body:
            return all_other_roles

    # 3. 老闆訊息（非全體）：主管 supervisor 一定回覆統籌，其他角色命中才回覆
    if speaker == "boss":
        targets = []
        for r in all_other_roles:
            if r == "supervisor" or match_role_keywords(r, body):
                targets.append(r)
        return [r for r in ROLES_CONFIG.keys() if r in targets]

    # 4. 成員訊息：只回覆被 @提及／點名／命中關鍵字的角色；若無人命中，主管在含「進度／匯報／主管」時才回覆，否則零回覆
    non_sup_matched = [r for r in all_other_roles if r != "supervisor" and match_role_keywords(r, body)]
    if non_sup_matched:
        if ("@supervisor" in body.lower() or "主管" in body) and "supervisor" in all_other_roles:
            targets = non_sup_matched + ["supervisor"]
        else:
            targets = non_sup_matched
        return [r for r in ROLES_CONFIG.keys() if r in targets]
    else:
        # 若無其他成員命中，主管在含「進度／匯報／主管」時才回覆，否則零回覆
        if "supervisor" in all_other_roles and match_role_keywords("supervisor", body):
            return ["supervisor"]
        return []


def load_role_prompt(role: str, max_chars: int = 600) -> str:
    """載入 roles/{role}.md 的說明，若找不到則使用預設說明，上限截斷至 max_chars (預設 600 字)"""
    role_file = Path("roles") / f"{role}.md"
    content = ""
    if role_file.exists():
        try:
            content = role_file.read_text(encoding="utf-8")
        except Exception:
            pass
    if not content:
        cfg = ROLES_CONFIG.get(role, {})
        content = f"{cfg.get('name', role)}: {cfg.get('title', '')}"
    if len(content) > max_chars:
        content = content[:max_chars]
    return content


def generate_llm_reply(role: str, message: dict, recent_history: list) -> str:
    """透過 OpenAI 相容端點請求 LLM 回覆（若有配置金鑰）"""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("MODEL", "gpt-4o-mini").strip()

    if not api_key:
        return ""

    role_desc = load_role_prompt(role, max_chars=600)
    system_prompt = (
        f"你是團隊中的成員：{ROLES_CONFIG.get(role, {}).get('name', role)}。\n"
        f"你的職責定義如下：\n{role_desc}\n\n"
        "請根據最新訊息與歷史對話，以符合你角色立場的角度進行簡潔繁體中文回覆（2至3句話以內）。"
        "如果你認為此訊息不需要你回覆或與你職責無關，請僅回傳「SILENT」。"
    )

    # 歷史最多 6 條，每條 body 截斷至 200 字
    history_lines = []
    for m in recent_history[-6:]:
        spk = m.get("speaker", "")
        b = m.get("body", "")
        if len(b) > 200:
            b = b[:200]
        history_lines.append(f"{spk}: {b}")
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
    role_dict = DEMO_REPLIES.get(role, {})

    if speaker == "boss":
        return role_dict.get("boss", "")

    # 成員互相交流：若命中職能或提及
    if match_role_keywords(role, body):
        return role_dict.get("general", "")

    return ""


def dispatch_message(incoming_msg: dict, conn: sqlite3.Connection) -> list:
    """
    根據分流規則派送新訊息給符合條件的角色，收集回覆並寫入資料庫
    回傳新增的回覆清單
    """
    speaker = incoming_msg["speaker"]
    msg_id = incoming_msg["id"]
    body = incoming_msg["body"]
    kind = incoming_msg["kind"]

    # 分流決定目標接收角色（在調用 LLM 之前先做）
    target_roles = get_target_roles(incoming_msg)
    if not target_roles:
        return []

    # 取得最近對話脈絡（LLM 歷史上限 6 條，節省 context token）
    cur = conn.cursor()
    cur.execute("SELECT speaker, body FROM messages WHERE id <= ? ORDER BY id DESC LIMIT 6", (msg_id,))
    recent_history = [dict(r) for r in reversed(cur.fetchall())]

    has_llm = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    generated_replies = []

    for role in target_roles:
        reply_text = ""
        if has_llm:
            reply_text = generate_llm_reply(role, incoming_msg, recent_history)
            # SILENT 即真靜默，絕不退回 demo 回覆
        else:
            # Demo 模式（無 key）：僅對分流命中角色產生 demo 回覆
            reply_text = generate_demo_reply(role, incoming_msg)

        if reply_text and reply_text.strip():
            now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reply_kind = "command_reply" if kind in ("command", "boss") or speaker == "boss" else "chat"
            with conn:
                cur = conn.execute(
                    "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                    (role, reply_text.strip(), now_iso, reply_kind)
                )
                new_id = cur.lastrowid
                generated_replies.append({
                    "id": new_id,
                    "speaker": role,
                    "body": reply_text.strip(),
                    "created_at": now_iso,
                    "kind": reply_kind
                })

    return generated_replies


def merge_burst_messages(unprocessed: list) -> list:
    """
    同批突發合併：同一輪 run_once 入面，若同一發言者連續發多條、其間無其他發言者插隊，
    只對最後一條做一次分流（body 用換行合併），避免連發刷爆調用次數。
    單條訊息的舊行為不變。
    """
    if not unprocessed:
        return []

    merged = []
    current_group = [unprocessed[0]]

    for msg in unprocessed[1:]:
        if msg["speaker"] == current_group[-1]["speaker"]:
            current_group.append(msg)
        else:
            merged.append(_combine_group(current_group))
            current_group = [msg]

    if current_group:
        merged.append(_combine_group(current_group))

    return merged


def _combine_group(group: list) -> dict:
    if len(group) == 1:
        return group[0]
    last_msg = group[-1]
    combined_body = "\n".join(m["body"] for m in group)
    return {
        "id": last_msg["id"],
        "speaker": last_msg["speaker"],
        "body": combined_body,
        "created_at": last_msg["created_at"],
        "kind": last_msg["kind"],
    }


def run_once(db_path: str = DEFAULT_DB_PATH, watermark_path: str = DEFAULT_WATERMARK_PATH) -> list:
    """
    執行單次輪詢檢查：
    1. 讀取 watermark
    2. 查詢未處理訊息 (id > watermark)
    3. 合併同發言者連續突發訊息
    4. 依分流規則派送處理，更新 watermark
    5. 回傳所有新產生的回覆訊息
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

    highest_processed_id = max(last_id, max(m["id"] for m in unprocessed))

    # 同批突發合併
    dispatch_queue = merge_burst_messages(unprocessed)

    all_new_replies = []
    for msg in dispatch_queue:
        replies = dispatch_message(msg, conn)
        all_new_replies.extend(replies)

    if all_new_replies:
        max_reply_id = max(r["id"] for r in all_new_replies)
        highest_processed_id = max(highest_processed_id, max_reply_id)

    save_watermark(highest_processed_id, watermark_path)
    conn.close()
    return all_new_replies


def poll_loop(db_path: str = DEFAULT_DB_PATH, watermark_path: str = DEFAULT_WATERMARK_PATH, interval: float = DEFAULT_POLL_INTERVAL):
    """常駐輪詢主迴圈"""
    routing_mode = "啟用 (ROUTING_ENABLED=1)" if os.environ.get("ROUTING_ENABLED", "1").strip().lower() not in ("0", "false", "no") else "停用 (全員回覆)"
    print(f"[RELAY] 啟動自動中繼輪詢服務 (間隔: {interval}s)")
    print(f"[RELAY] 資料庫路徑: {db_path}")
    print(f"[RELAY] 浮水印檔案: {watermark_path}")
    print(f"[RELAY] 智能分流: {routing_mode}")
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
