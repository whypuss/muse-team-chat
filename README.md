# Muse Team Chat — 團隊自動協作群聊系統

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: Standard Library Only](https://img.shields.io/badge/Python-3.8+-brightgreen.svg)](https://www.python.org/)
[![Zero External Dependencies](https://img.shields.io/badge/Dependencies-Zero%20(Standard%20Lib)-success.svg)](server.py)

一個為多智能體團隊設計的**真正自主互相交流群聊系統**。純 Python 標準庫打造，無任何第三方 pip 依賴，開箱即用。

---

## 為什麼需要 Muse Team Chat？

以往在 Muse 原生環境（Side Chat ＋ Artifact 聊天室 ＋ 15 分鐘 Cron）中，存在一個根本性架構痛點：
- **各自孤立**：每位成員各自住在獨立的對話窗口中，無法主動感知群聊天室的新內容。
- **依賴人工搬運**：老闆發布指令需要項目主管手動複製給成員；成員回覆後又要手動搬回 Artifact，過程繁瑣延遲。
- **假死空虛感**：聊天室常顯得空無一人、無人即時回應。

**Muse Team Chat 徹底解決了這個問題**：
1. **單一中央訊息庫**：基於 SQLite（`messages` 表），所有發言集中寫入，不再各自散落。
2. **自動中繼與浮水印輪詢（`relay.py`）**：預設每 5 秒自動檢查新訊息，透過 `data/watermark.json` 記錄已處理進度，保證不重複派送。
3. **全員全自動響應**：任何一位成員（包括老闆）一發言，新訊息自動派送給其餘所有成員；成員根據職能產生回覆並自動寫回同一個資料庫。
4. **同場即時 Web 界面**：同一畫面看見所有人交流，各有名字、身分標籤、獨立色標與對話框佈局，**全程零手動複製貼上**。

---

## 團隊成員角色與視覺規範

本系統預設配置六位核心角色，各司其職且在介面中有專屬色系與佈局：

| 角色 | 代號 | 代表顏色 | 介面佈局 | 核心職責 |
| :--- | :--- | :--- | :--- | :--- |
| **老闆（歪貓）** | `boss` | 金色 (`#D4AF37`) | 置中（含「老闆指令」標籤與專屬輸入框） | 最終拍板、旁觀全程 |
| **項目主管（pj主管）** | `supervisor` | 深青色 (`#008B8B`) | 靠左 | 統籌、整理、向老闆匯報 |
| **UIUX 設計師** | `designer` | 柿紅色 (`#E25822`) | 靠右 | 設計資產、skill、品牌視覺，UIUX＋3D 合併一人 |
| **tin 軟件工程師** | `tin` | 鋼藍色 (`#4682B4`) | 靠左 | 唔自己寫 code，搞清需求、拆任務、派俾免費工具寫 code、再驗收 |
| **cin 驗收師** | `cin` | 紫色 (`#8A2BE2`) | 靠右 | 軟件安全性＋功能驗收，派工前先用真任務探針自己試，唔淨引舊記錄、唔淨靠 PONG |
| **Money 額度管理** | `money` | 草綠色 (`#2E8B57`) | 靠左 | 管 agy、Kilo、OpenCode、SenseNova、VS Code 額度同免費任務排程，邊個有額度用邊個，卡死就降級或換工具 |

角色詳細設定與 Prompt 說明請參閱 [`roles/`](roles/) 目錄。

---

## 系統架構

```mermaid
flowchart TD
    User["用戶 / 老闆"] -->|1. Web 介面發布指令| WebServer["server.py (Web UI + REST API)"]
    WebServer -->|2. 寫入發言| DB[("中央訊息庫 SQLite\n(data/chat.db)")]
    
    subgraph 自動中繼引擎["Relay 自動中繼輪詢 (relay.py)"]
        Poll["定時輪詢 (預設 5s)\n讀取 id > Watermark"]
        WM[("data/watermark.json\n防重複派送機制")]
        Engine{"判斷發言者與目標角色"}
        LLM["可插拔 LLM (OpenAI API)\n或 內建角色規則 Demo"]
        
        Poll <--> WM
        Poll --> DB
        DB --> Engine
        Engine --> LLM
        LLM -->|3. 自動產生回覆寫回| DB
    end

    DB -.->|4. 自動輪詢更新 (2.5s)| WebUI["手機/桌面端同場即時群聊畫面"]
```

---

## 檔案結構

```
muse-team-chat/
├── .gitignore               # 排除 SQLite 資料庫、金鑰與臨時檔
├── .env.example             # 環境變數範本（絕不包含真實金鑰）
├── LICENSE                  # MIT 授權條款
├── README.md                # 專案中文說明文件
├── server.py                # Web 介面與 REST API 伺服器
├── relay.py                 # 自動中繼與輪詢派送核心
├── data/
│   └── .gitkeep             # 執行期存放 chat.db 與 watermark.json
├── docs/
│   └── muse-adaptation.md   # Muse 原生架構限制與獨立版適配說明
├── roles/                   # 各成員職責定義 Markdown
│   ├── boss.md              # 老闆（歪貓）
│   ├── supervisor.md        # 項目主管
│   ├── designer.md          # UIUX 設計師
│   ├── tin.md               # tin 軟件工程師
│   ├── cin.md               # cin 驗收師
│   └── money.md             # Money 額度管理
└── tests/                   # 自動化測試套件
    ├── test_relay.py        # 輪詢、派送、浮水印與防重複測試
    └── test_server.py       # Web API 與端對端流通測試
```

---

## 快速上手

### 1. 環境需求
- Python 3.8 或以上版本。
- **無需安裝任何 pip 依賴套件**（全使用標準庫 `http.server`、`sqlite3`、`urllib` 等）。

### 2. 啟動伺服器
直接運行 `server.py`，預設會同時啟動 Web UI 與背景自動中繼輪詢：
```bash
python3 server.py
```
啟動後打開瀏覽器訪問：
👉 **http://localhost:8080**

### 3. 發送第一條訊息
- 在網頁上方「👑 老闆指令發布」框中輸入任何任務或問題，點擊「發布指令 ⚡」。
- 系統將訊息寫入資料庫，`relay` 模組在 5 秒內（或即時觸發）派送給主管、tin、cin、money、designer 各成員。
- 介面自動呈現每位成員的專業回覆！

---

## 運行模式與配置

系統支援複製 `.env.example` 為 `.env` 或透過環境變數自訂：

```bash
cp .env.example .env
```

| 變數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `PORT` | `8080` | Web 伺服器連接埠 |
| `DB_PATH` | `data/chat.db` | SQLite 中央資料庫路徑 |
| `WATERMARK_PATH` | `data/watermark.json` | 浮水印記錄檔案路徑 |
| `POLL_INTERVAL` | `5` | 輪詢檢查間隔秒數 |
| `OPENAI_BASE_URL` | *(空)* | 可選：相容 OpenAI 的 API 端點（如 Ollama、OpenRouter） |
| `OPENAI_API_KEY` | *(空)* | 可選：API 金鑰。**若留空則自動以角色規則 Demo 模式運行** |
| `MODEL` | `gpt-4o-mini` | LLM 模型名稱 |

> [!NOTE]
> **可插拔 LLM 設計**：未設定 `OPENAI_API_KEY` 時，系統自動以內建角色職能規則運行 Demo 模式，不硬編造假資料、不扮假 LLM，亦絕不依賴任何外部網絡連線。

---

## 命令列獨立運行

除了整合式 `server.py`，你也可以獨立使用中繼工具：

- **單次執行（適合 Cron 或 CI 測試）**：
  ```bash
  python3 relay.py --once
  ```
- **獨立常駐輪詢進程**：
  ```bash
  python3 relay.py
  ```
- **重設浮水印**：
  ```bash
  python3 relay.py --reset-watermark
  ```

---

## 執行測試套件

執行所有單元與端對端測試：

```bash
python3 -m unittest discover -s tests -v
```

測試涵蓋：
- 老闆發布指令後，全體成員自動派送與回覆寫入。
- Watermark 防重複機制（再次輪詢不產生重複回覆）。
- 成員互相點名 (@cin, @tin 等) 互動不陷入無窮迴圈。
- Web API 端點（HTML 首頁、`/api/members`、`/api/messages`、`/api/health`）。

---

## 安全聲明

本專案遵循嚴格安全規範：
- 絕無寫入任何真實 API 金鑰、Token、密碼或專屬憑證。
- 絕無硬編碼任何私人項目細節或真實商業對話。
- `.gitignore` 嚴格排除 `*.db`、`*.sqlite`、`.env`、金鑰檔與各類日誌。

---

## 授權

本專案採用 [MIT License](LICENSE) 條款開放。
