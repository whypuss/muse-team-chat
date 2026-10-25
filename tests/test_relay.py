#!/usr/bin/env python3
"""
tests/test_relay.py - Muse Team Chat 自動中繼與輪詢機制測試
驗證：
1. 老闆發言 -> relay 跑一次 -> 其餘成員收到並自動回覆入庫
2. Watermark 防止重複派送
3. 輪詢防無限迴圈
4. 成員相互提及 (@cin / @tin 等) 正確響應
5. 損壞 watermark 自我修復
"""

import unittest
import os
import sys
import json
import sqlite3
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

# 將上一層目錄加入 sys.path 以匯入 relay 和 server
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import relay


class TestMuseTeamChatRelay(unittest.TestCase):

    def setUp(self):
        # 建立獨立臨時測試目錄，避免污染真實環境
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_chat.db")
        self.watermark_path = os.path.join(self.test_dir, "watermark.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_database_initialization(self):
        """測試資料庫與 messages 表結構建立"""
        conn = relay.get_db_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row, "messages 資料表應被正確建立")

    def test_watermark_load_and_save(self):
        """測試浮水印讀取與寫入，以及空值/損毀時的容錯"""
        # 未建立檔案時應回傳 0
        self.assertEqual(relay.load_watermark(self.watermark_path), 0)

        # 寫入特定 ID
        relay.save_watermark(42, self.watermark_path)
        self.assertEqual(relay.load_watermark(self.watermark_path), 42)

        # 測試損毀 JSON 容錯恢復為 0
        with open(self.watermark_path, "w", encoding="utf-8") as f:
            f.write("{invalid json format... ")
        self.assertEqual(relay.load_watermark(self.watermark_path), 0)

    def test_boss_message_dispatches_to_all_members_and_updates_watermark(self):
        """
        核心驗收測試：
        老闆發一條訊息 → relay 跑一次 → 其餘 5 位成員收到 → 成員回覆寫入庫 →
        watermark 更新 → 再次跑 relay 唔會重複派送同一條訊息
        """
        conn = relay.get_db_connection(self.db_path)

        # 1. 老闆發一條指令
        with conn:
            cur = conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("boss", "全體注意：啟動新產品架構評估，請各成員依職能分工響應。", "2026-10-25 10:00:00", "command")
            )
            boss_msg_id = cur.lastrowid
        conn.close()

        self.assertEqual(boss_msg_id, 1)

        # 2. 跑一次 relay
        replies = relay.run_once(self.db_path, self.watermark_path)

        # 應有其餘 5 位成員自動產生回覆（supervisor, designer, tin, cin, money）
        expected_roles = {"supervisor", "designer", "tin", "cin", "money"}
        reply_speakers = {r["speaker"] for r in replies}

        self.assertEqual(len(replies), 5, f"應產生 5 條回覆，實際產生 {len(replies)}")
        self.assertEqual(reply_speakers, expected_roles, "5 位成員均應響應老闆指令")

        # 檢查資料庫內總訊息數應為 1 (老闆) + 5 (成員回覆) = 6
        conn = relay.get_db_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT id, speaker, body FROM messages ORDER BY id ASC")
        all_msgs = [dict(r) for r in cur.fetchall()]
        conn.close()

        self.assertEqual(len(all_msgs), 6)
        self.assertEqual(all_msgs[0]["speaker"], "boss")

        # 3. 檢查浮水印已更新
        watermark = relay.load_watermark(self.watermark_path)
        max_id = all_msgs[-1]["id"]
        self.assertGreaterEqual(watermark, max_id, "浮水印應已推進至最新處理訊息")

        # 4. 再次跑 relay 測試防重派送
        duplicate_replies = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(duplicate_replies), 0, "同一條訊息不可被重複派送或重複產生回覆")

        # 資料庫內訊息總數依然保持 6
        conn = relay.get_db_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM messages")
        count_after = cur.fetchone()["total"]
        conn.close()
        self.assertEqual(count_after, 6, "防重派送生效，資料庫訊息總數不應增加")

    def test_member_mention_interaction(self):
        """測試成員間互相交流（例如提及 @cin）能觸發對應成員回覆且不陷入死循環"""
        conn = relay.get_db_connection(self.db_path)

        # 先推進 watermark 避免既有訊息干擾
        relay.save_watermark(100, self.watermark_path)

        # tin 發起工程狀態並點名 cin
        with conn:
            conn.execute(
                "INSERT INTO messages (id, speaker, body, created_at, kind) VALUES (?, ?, ?, ?, ?)",
                (101, "tin", "工程模組骨架已產出，請 @cin 驗收師執行探針測試。", "2026-10-25 10:15:00", "chat")
            )
        conn.close()

        # 跑一次 relay
        replies = relay.run_once(self.db_path, self.watermark_path)
        self.assertGreaterEqual(len(replies), 1)
        speakers = [r["speaker"] for r in replies]
        self.assertIn("cin", speakers, "cin 應在被提及時主動回應探針驗收狀態")

        # 再次跑 relay，確認 watermark 已更新，不會重複觸發
        second_replies = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(second_replies), 0)

    def test_roles_config_completeness(self):
        """測試六位成員名單與設定完整性"""
        required_roles = ["boss", "supervisor", "designer", "tin", "cin", "money"]
        for role in required_roles:
            self.assertIn(role, relay.ROLES_CONFIG, f"成員 {role} 必須在 ROLES_CONFIG 中定義")
            cfg = relay.ROLES_CONFIG[role]
            self.assertIn("name", cfg)
            self.assertIn("color", cfg)
            self.assertIn("align", cfg)
            self.assertIn("badge", cfg)

        # 驗證色彩與對齊規範
        self.assertEqual(relay.ROLES_CONFIG["boss"]["align"], "center")
        self.assertEqual(relay.ROLES_CONFIG["boss"]["color"].lower(), "#d4af37")
        self.assertEqual(relay.ROLES_CONFIG["supervisor"]["align"], "left")
        self.assertEqual(relay.ROLES_CONFIG["designer"]["align"], "right")
        self.assertEqual(relay.ROLES_CONFIG["tin"]["align"], "left")
        self.assertEqual(relay.ROLES_CONFIG["cin"]["align"], "right")
        self.assertEqual(relay.ROLES_CONFIG["money"]["align"], "left")

    def test_boss_normal_message_only_supervisor_replies(self):
        """測試老闆普通非全體訊息：只有主管 supervisor 回覆統籌，其他成員不回覆"""
        conn = relay.get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("boss", "今日伺服器已完成定期冷備份，特此存檔。", "2026-10-25 11:00:00", "command")
            )
        conn.close()

        replies = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(replies), 1, "老闆普通訊息應僅有主管 1 人回覆統籌")
        self.assertEqual(replies[0]["speaker"], "supervisor")

    def test_boss_mention_cin_only_supervisor_and_cin_reply(self):
        """測試老闆點名 @cin 訊息：僅主管與 cin 回覆，designer/tin/money 不回覆"""
        conn = relay.get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("boss", "@cin 請針對最新 API 模組執行安全探針驗收。", "2026-10-25 11:10:00", "command")
            )
        conn.close()

        replies = relay.run_once(self.db_path, self.watermark_path)
        speakers = {r["speaker"] for r in replies}
        self.assertEqual(len(replies), 2, f"應僅有 2 條回覆 (主管與 cin)，實際產生 {len(replies)}")
        self.assertEqual(speakers, {"supervisor", "cin"})
        self.assertNotIn("designer", speakers, "designer 不應回覆")
        self.assertNotIn("tin", speakers, "tin 不應回覆")
        self.assertNotIn("money", speakers, "money 不應回覆")

    def test_silence_keyword_produces_zero_replies(self):
        """測試含「不用回覆／不用回／靜默」時全部靜默，零回覆零調用"""
        conn = relay.get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("boss", "全體注意：今日下午機房線路檢修，本通知不用回覆。", "2026-10-25 11:20:00", "command")
            )
        conn.close()

        replies = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(replies), 0, "含『不用回覆』時應完全零回覆")

        # 檢查資料庫僅有老闆原訊息，無新增回覆
        conn = relay.get_db_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM messages")
        self.assertEqual(cur.fetchone()["total"], 1)
        conn.close()

        # 測試成員使用「靜默」
        with relay.get_db_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("tin", "代碼版本已凍結，靜默處理。", "2026-10-25 11:21:00", "chat")
            )
        replies_silent = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(replies_silent), 0, "含『靜默』時應完全零回覆")

    def test_member_normal_chat_and_report_progress(self):
        """測試成員閒聊零回覆，成員匯報進度僅主管回覆"""
        conn = relay.get_db_connection(self.db_path)
        # 1. 無人命中之成員閒聊 -> 零回覆
        with conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("tin", "今天天氣轉涼，大家注意保暖。", "2026-10-25 11:30:00", "chat")
            )
        conn.close()

        # 注意：「大家」在閒聊中是廣播關鍵字；換一個無關鍵字閒聊
        with relay.get_db_connection(self.db_path) as conn:
            conn.execute("DELETE FROM messages")
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("tin", "今天中午去茶餐廳吃飯。", "2026-10-25 11:30:00", "chat")
            )
        relay.save_watermark(0, self.watermark_path)

        replies = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(replies), 0, "成員無關鍵字發言應零回覆")

        # 2. 成員向主管匯報進度 -> 僅主管回覆
        with relay.get_db_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("tin", "核心工作已理清，向主管匯報進度更新。", "2026-10-25 11:35:00", "chat")
            )
        replies2 = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(replies2), 1, "成員匯報進度應僅主管回覆")
        self.assertEqual(replies2[0]["speaker"], "supervisor")

    def test_burst_merge_consecutive_messages(self):
        """測試同批突發合併：同發言者連續發多條只對最後一條做一次分流（換行合併）"""
        conn = relay.get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO messages (id, speaker, body, created_at, kind) VALUES (?, ?, ?, ?, ?)",
                (201, "boss", "第一條：提醒一下", "2026-10-25 12:00:00", "command")
            )
            conn.execute(
                "INSERT INTO messages (id, speaker, body, created_at, kind) VALUES (?, ?, ?, ?, ?)",
                (202, "boss", "第二條：@cin 請針對剛發布的補丁執行探針測試", "2026-10-25 12:00:05", "command")
            )
        conn.close()
        relay.save_watermark(200, self.watermark_path)

        replies = relay.run_once(self.db_path, self.watermark_path)
        # 連續兩條被合併為一條派送，目標為 supervisor + cin，回覆應為 2 條
        speakers = {r["speaker"] for r in replies}
        self.assertEqual(len(replies), 2, "突發訊息應合併為一次分流，產生 2 條回覆 (主管+cin)")
        self.assertEqual(speakers, {"supervisor", "cin"})

        # 確認浮水印已覆蓋 202 及所有回覆
        wm = relay.load_watermark(self.watermark_path)
        self.assertGreaterEqual(wm, 202)

        # 再次輪詢不產生任何新回覆
        second = relay.run_once(self.db_path, self.watermark_path)
        self.assertEqual(len(second), 0)

    def test_prompt_and_history_limits(self):
        """測試 Prompt 截斷至 600 字與歷史截斷上限"""
        # 測試 load_role_prompt 截斷
        long_text = "職責說明" * 200  # 800 字
        roles_dir = BASE_DIR / "roles"
        test_role_file = roles_dir / "test_dummy.md"
        try:
            test_role_file.write_text(long_text, encoding="utf-8")
            prompt = relay.load_role_prompt("test_dummy", max_chars=600)
            self.assertLessEqual(len(prompt), 600)
        finally:
            if test_role_file.exists():
                test_role_file.unlink()

        # 測試 history 每條截斷 200 字
        history = [
            {"speaker": "boss", "body": "A" * 300},
            {"speaker": "cin", "body": "B" * 300},
        ]
        # 無 key 狀態調用 generate_llm_reply 應優雅回傳空字串
        self.assertEqual(relay.generate_llm_reply("supervisor", {"body": "test"}, history), "")

    def test_llm_silent_does_not_fallback_to_demo(self):
        """測試 LLM 回傳 SILENT 時為真靜默，絕不退回 demo 回覆"""
        from unittest.mock import patch, MagicMock

        # 模擬 OpenAI API 回應 SILENT
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "SILENT"}}]
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-mock-key-for-test"}):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                conn = relay.get_db_connection(self.db_path)
                msg = {
                    "id": 999,
                    "speaker": "boss",
                    "body": "全體注意：請各角色評估新功能。",
                    "kind": "command"
                }
                # 調用 dispatch_message
                replies = relay.dispatch_message(msg, conn)
                conn.close()

                # 當 LLM 回傳 SILENT 時，回覆應為 0 條，不得退回 demo 回覆
                self.assertEqual(len(replies), 0, "LLM 回傳 SILENT 時必須保持真靜默，不得退回 demo")

    def test_routing_disabled_env(self):
        """測試當 ROUTING_ENABLED=0 時，還原舊版全員回覆行為"""
        conn = relay.get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO messages (speaker, body, created_at, kind) VALUES (?, ?, ?, ?)",
                ("boss", "普通工作日誌存檔。", "2026-10-25 13:00:00", "command")
            )
        conn.close()

        with patch.dict(os.environ, {"ROUTING_ENABLED": "0"}):
            replies = relay.run_once(self.db_path, self.watermark_path)
            self.assertEqual(len(replies), 5, "ROUTING_ENABLED=0 時應還原為全員 5 人回覆")


if __name__ == "__main__":
    unittest.main()
