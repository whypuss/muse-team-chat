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


if __name__ == "__main__":
    unittest.main()
