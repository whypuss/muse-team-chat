#!/usr/bin/env python3
"""
tests/test_server.py - 測試 server.py 的 HTTP 介面與 REST API
"""

import unittest
import os
import sys
import json
import sqlite3
import tempfile
import shutil
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from http.server import HTTPServer

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import relay
import server


class TestServerAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.test_dir, "test_server.db")
        cls.watermark_path = os.path.join(cls.test_dir, "watermark.json")

        # 設定環境變數覆寫
        server.DB_PATH = cls.db_path
        server.WATERMARK_PATH = cls.watermark_path
        relay.DEFAULT_DB_PATH = cls.db_path
        relay.DEFAULT_WATERMARK_PATH = cls.watermark_path

        # 初始化資料庫
        conn = relay.get_db_connection(cls.db_path)
        conn.close()

        # 在隨機/空閒連接埠啟動測試伺服器
        cls.port = 18888
        cls.httpd = HTTPServer(("127.0.0.1", cls.port), server.ChatRequestHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_get_root_html(self):
        """測試根路徑回傳 HTML 網頁且包含主要視覺元素"""
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")
            self.assertIn("Muse Team Chat", body)
            self.assertIn("老闆指令發布", body)

    def test_get_members_api(self):
        """測試 /api/members API 回傳 6 位成員"""
        url = f"http://127.0.0.1:{self.port}/api/members"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data), 6)
            self.assertIn("boss", data)
            self.assertIn("supervisor", data)
            self.assertIn("designer", data)
            self.assertIn("tin", data)
            self.assertIn("cin", data)
            self.assertIn("money", data)

    def test_health_api(self):
        """測試 /api/health 健康狀態端點"""
        url = f"http://127.0.0.1:{self.port}/api/health"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "healthy")
            self.assertIn("message_count", data)
            self.assertIn("watermark", data)

    def test_post_message_and_fetch_messages(self):
        """測試 POST /api/messages 與 GET /api/messages 端到端流程"""
        url = f"http://127.0.0.1:{self.port}/api/messages"
        payload = {
            "speaker": "boss",
            "body": "全體測試命令：全面啟動端對端流程驗收。",
            "kind": "command"
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 201)
            created = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(created["speaker"], "boss")
            self.assertEqual(created["body"], payload["body"])

        # 查詢所有訊息，確認老闆發言與成員自動回覆均已寫入庫
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, 200)
            messages = json.loads(resp.read().decode("utf-8"))
            self.assertGreaterEqual(len(messages), 6)
            speakers = [m["speaker"] for m in messages]
            self.assertIn("boss", speakers)
            self.assertIn("supervisor", speakers)
            self.assertIn("tin", speakers)
            self.assertIn("cin", speakers)


if __name__ == "__main__":
    unittest.main()
