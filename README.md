# LINE 分帳機器人 v1.0

一個專為 LINE 群組設計的智能分帳機器人，提供完整的費用分攤、債務追蹤和結算功能。

## ✨ 功能特色

### 🆕 創建支出

- **均攤模式**：自動將費用平均分攤給所有參與人（無條件進位至整數）
- **分別計算**：支援每人負擔不同金額的靈活分攤
- **代墊功能**：付款人可代墊全額，其他人承擔指定金額
- **智能防重複**：資料庫層面防護，避免重複建立相同帳單
- **群組隔離**：不同群組間的帳單完全隔離，確保數據安全

### 📊 查看功能

- **帳單列表**：美觀的帳單總覧，顯示付款進度
- **帳單詳情**：完整的帳單資訊和參與人狀況
- **個人欠款**：Flex Message 視覺化介面顯示個人債務
- **群組欠款**：群組所有成員欠款總覧（按金額排序）

### 💳 結帳功能

- **標記付款**：付款人可標記已付款成員
- **自動更新**：即時更新付款狀態和時間戳記
- **權限控制**：只有付款人可執行結帳操作

### 🗑️ 結算功能

- **個人結算**：刪除個人所有付款帳單，釋放資料庫空間
- **群組結算**：清空群組所有帳單記錄，完全重置
- **結算報告**：提供詳細的刪除統計和狀況報告

### 🎨 視覺化介面

- **Flex Message 主選單**：美觀的卡片式操作介面
- **帳單建立精靈**：引導式的帳單創建流程
- **響應式設計**：適配不同裝置的顯示效果

### 🔒 安全機制

- **原子性操作**：使用資料庫事務確保數據一致性
- **重複操作防護**：防止在短時間內重複執行相同操作
- **內容 Hash 檢查**：防止相同內容的帳單重複建立

## 🛠️ 系統要求

- **Python**: 3.11+
- **資料庫**: PostgreSQL (生產環境) / SQLite (開發環境)
- **LINE**: LINE Bot API 帳號
- **部署平台**: 支援 Flask 的任何平台（如 Heroku、Railway 等）

## 📦 安裝指南

### 1. 克隆專案

```bash
git clone https://github.com/your-username/line-split-bot.git
cd line-split-bot
```

### 2. 安裝依賴

#### 使用 Poetry（推薦）

```bash
pip install poetry
poetry install
```

#### 使用 pip

```bash
pip install -r requirements.txt
```

### 3. 環境變數設定

創建 `.env` 文件：

```env
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
LINE_CHANNEL_SECRET=your_line_channel_secret
DATABASE_URL=your_postgresql_url  # 生產環境
```

### 4. 資料庫初始化

```bash
python -c "from models_splitbill import init_db_splitbill; init_db_splitbill()"
```

### 5. 啟動應用

#### 開發環境

```bash
python app_splitbill.py
```

#### 生產環境

```bash
gunicorn app_splitbill:app --bind 0.0.0.0:$PORT
```

## 📖 使用方法

### 基本指令

#### 創建支出

**均攤模式**（所有人平均分攤）：

```text
#新增支出 300 午餐 @小美 @小王
```

- 您和 2 位朋友均攤，每人 100 元
- 付款人會自動參與分攤計算

**分別計算模式**（指定每人金額）：

```text
#新增支出 1000 聚餐 @小美 400 @小王 350
```

- 您負擔剩餘 250 元，小美 400 元，小王 350 元

**代墊功能**（付款人代墊全額）：

```text
#新增支出 500 代付款 @小美 300 @小王 200
```

- 您代墊 500 元，小美欠您 300 元，小王欠您 200 元

#### 查看功能

```text
#帳單列表           # 查看所有未封存帳單
#支出詳情 B-1       # 查看特定帳單詳情
#我的欠款           # 查看個人欠款（Flex 介面）
#群組欠款           # 查看群組所有成員欠款總覧
```

#### 結帳功能

```text
#結帳 B-1 @小美 @小王    # 標記小美和小王已付款
#封存帳單 B-1            # 手動封存已結清帳單
```

#### 結算功能

```text
#個人結算    # 刪除個人所有付款帳單（清理資料庫）
#群組結算    # 刪除群組所有帳單（完全重置）
```

#### 視覺化介面

```text
#選單        # 主選單（Flex Message）
#建立帳單    # 帳單建立精靈
#幫助        # 完整使用說明
```

### 重要說明

- **付款人自動參與**：記帳的人會自動參與分攤，無需 @自己
- **無條件進位**：均攤模式採用無條件進位，避免小數點
- **群組隔離**：不同群組的帳單完全獨立
- **權限控制**：只有付款人可以執行結帳和封存操作
- **防重複機制**：系統會自動防止重複建立相同內容的帳單

## 🏗️ 技術架構

### 後端技術棧

- **Flask**: Web 框架
- **SQLAlchemy**: ORM 資料庫操作
- **LINE Bot SDK**: LINE 機器人 API
- **PostgreSQL**: 主要資料庫
- **Python-dotenv**: 環境變數管理

### 資料庫設計

```sql
-- 群組成員表
GroupMember {
    id: 主鍵
    line_user_id: LINE 用戶 ID
    group_id: 群組 ID
    name: 顯示名稱
    created_at: 建立時間
}

-- 帳單表
Bill {
    id: 主鍵
    group_id: 群組 ID
    description: 支出說明
    total_bill_amount: 總金額
    payer_member_id: 付款人 ID
    split_type: 分攤類型 (EQUAL/UNEQUAL)
    content_hash: 內容雜湊值
    is_archived: 是否封存
    created_at: 建立時間
}

-- 帳單參與人表
BillParticipant {
    id: 主鍵
    bill_id: 帳單 ID
    debtor_member_id: 欠款人 ID
    amount_owed: 欠款金額
    is_paid: 是否已付
    paid_at: 付款時間
}

-- 重複操作防護表
DuplicatePreventionLog {
    id: 主鍵
    operation_hash: 操作雜湊值
    group_id: 群組 ID
    user_line_id: 用戶 LINE ID
    operation_type: 操作類型
    created_at: 建立時間
}
```

### 核心功能模組

- **models_splitbill.py**: 資料模型和資料庫操作
- **app_splitbill.py**: 主應用程式和 LINE 訊息處理
- **原子性操作**: 防止併發問題的資料庫約束
- **Flex Message**: 美觀的視覺化介面

## 🚀 部署指南

### Heroku 部署

1.創建 Heroku 應用：

```bash
heroku create your-line-split-bot
```

2.設定環境變數：

```bash
heroku config:set LINE_CHANNEL_ACCESS_TOKEN=your_token
heroku config:set LINE_CHANNEL_SECRET=your_secret
```

3.添加 PostgreSQL：

```bash
heroku addons:create heroku-postgresql:mini
```

4.部署應用：

```bash
git push heroku main
```

### Railway 部署

1. 連接 GitHub 儲存庫
2. 設定環境變數
3. 添加 PostgreSQL 資料庫
4. 自動部署

### LINE Bot 設定

1. 在 [LINE Developers Console](https://developers.line.biz/) 建立機器人
2. 設定 Webhook URL: `https://your-app.herokuapp.com/splitbill/callback`
3. 開啟「Allow bot to join group chats」
4. 複製 Channel Access Token 和 Channel Secret

## 🧪 測試

### 執行完整測試

```bash
python test_complete_flow_v1.py
```

測試項目包含：

- **帳單創建**: 均攤、分別計算、代墊功能
- **查看功能**: 帳單列表、詳情、個人欠款
- **結帳功能**: 付款狀態更新
- **結算功能**: 個人結算、群組結算
- **防護機制**: 重複帳單防護
- **邊界測試**: 空群組、不存在用戶

### 測試結果示例

```text
🚀 開始完整流程測試 v1.0
================================================================================
✅ 創建群組成員: 4 人
💰 開始創建支出測試
✅ 均攤模式: 帳單 B-1 創建成功
✅ 分別計算: 帳單 B-2 創建成功
✅ 代墊功能: 帳單 B-3 創建成功
📊 查看功能測試
✅ 找到 3 筆活躍帳單
💳 結帳功能測試
✅ 部分成員付款完成
🗑️ 結算功能測試
✅ 個人結算完成: 刪除 1 筆帳單
✅ 群組結算完成: 刪除 2 筆帳單
🔒 防護機制測試
✅ 重複帳單防護正常運作
================================================================================
✅ 所有測試通過！系統運行正常。
```

## 📄 API 文檔

### Webhook 端點

```text
POST /splitbill/callback
```

接收 LINE 平台的訊息回調，處理用戶指令。

### 支援的訊息類型

- **文字訊息**: 處理所有指令
- **群組訊息**: 僅在群組中運作
- **Flex Message**: 回傳視覺化介面

### 錯誤處理

- **資料庫錯誤**: 自動回滾事務
- **LINE API 錯誤**: 優雅降級處理
- **用戶輸入錯誤**: 友善的錯誤提示
- **權限錯誤**: 清楚的權限說明

## 🔄 版本歷程

### v1.0 (2024) - 正式版

- ✅ 完整的分帳功能（均攤、分別計算、代墊）
- ✅ 視覺化 Flex Message 介面
- ✅ 原子性資料庫操作
- ✅ 重複操作防護機制
- ✅ 個人結算和群組結算功能
- ✅ 100% 測試覆蓋率
- ✅ 生產環境就緒

### 主要改進

- **效能優化**: 資料庫索引和查詢優化
- **安全強化**: 資料庫層面約束和事務控制
- **用戶體驗**: Flex Message 美觀介面
- **穩定性**: 全面的錯誤處理和日誌記錄

## 🤝 貢獻指南

1. Fork 專案
2. 創建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 開啟 Pull Request

### 開發規範

- 遵循 PEP 8 程式碼風格
- 添加適當的測試覆蓋率
- 更新相關文檔
- 確保所有測試通過

## 📞 支援與反饋

- **問題回報**: [GitHub Issues](https://github.com/your-username/line-split-bot/issues)
- **功能建議**: [GitHub Discussions](https://github.com/your-username/line-split-bot/discussions)
- **文檔問題**: 歡迎提交 PR 改善

## 📝 授權條款

此專案採用 MIT 授權條款 - 詳情請參閱 [LICENSE](LICENSE) 文件。

## 🎯 未來規劃

- [ ] 多語言支援（英文、日文）
- [ ] 匯出帳單為 CSV/PDF
- [ ] 定期結算提醒功能
- [ ] 統計圖表和分析功能
- [ ] 群組管理員權限控制

---

**LINE 分帳機器人 v1.0** - 讓群組分帳變得簡單、準確、美觀！ 🎉
