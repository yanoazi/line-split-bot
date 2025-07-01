# LINE分帳機器人 v2.8.2 🤖💰

一個專為LINE群組設計的智慧分帳機器人，提供完整的群組共同支出管理功能。

## ✨ 版本特色 (v2.8.2)

### 🎯 純粹的群組分帳計算邏輯

- 專注於群組成員間的債務計算
- 付款人自動參與分攤但不會欠自己錢
- 移除@自己相關的多餘程式碼（LINE不支援@自己）
- 簡潔直觀的分帳流程

### 💰 全面結算功能

- 🔥 **NEW** 一鍵智慧結算所有帳單
- 自動封存已結清的帳單
- 完整的結算報告
- 確保資料庫狀態一致性

### 🛡️ 強化的安全性

- 完善的重複操作防護機制
- 內容hash防止重複建立相同帳單
- 強化的群組資料隔離
- 完整的資料庫完整性保護

### 🎨 現代化用戶界面

- 美觀的Flex Message卡片式界面
- 直觀的操作流程
- 完整的指令幫助系統

## 🚀 核心功能

### 1. 💳 智慧分帳計算

- **均攤模式**：所有人平均分攤費用
- **分別計算模式**：每人負擔不同金額
- 付款人自動參與分攤計算
- 精確的小數點處理

### 2. 📊 帳單管理

- 建立和管理群組帳單
- 查看帳單詳情和參與人狀態
- 帳單列表和狀態追蹤
- 自動封存已結清帳單

### 3. 💸 債務追蹤

- 個人欠款查詢
- 付款狀態更新
- 結帳記錄追蹤

### 4. 🔒 群組隔離

- 完美的多群組資料隔離
- 同一用戶可在多個群組中使用
- 群組專屬的成員和帳單管理

### 5. 🛡️ 重複操作防護

- 智慧重複操作檢測
- 內容hash防重複建立
- 自動清理過期記錄

## 📱 使用方法

### 基本指令

#### 🆕 新增支出

```text
# 均攤模式 - 所有人平均分攤
#新增支出 300 午餐 @小美 @小王
# 結果：您和2位朋友均攤，每人100元

# 分別計算模式 - 每人不同金額
#新增支出 1000 聚餐 @小美 400 @小王 350
# 結果：您負擔剩餘250元，小美400元，小王350元
```

#### 📋 查看功能

```text
#帳單列表          # 查看所有帳單
#支出詳情 B-ID     # 查看特定帳單詳情
#我的欠款          # 查看個人未付款項
```

#### 💰 結算功能

```text
#結帳 B-ID @已付成員1 @成員2...  # 標記個別付款
#全面結算                        # 🔥 智慧結算所有帳單
#封存帳單 B-ID                   # 手動封存帳單
```

#### 🎨 視覺化選單

```text
#選單              # 主選單
#建立帳單          # 帳單建立精靈
#幫助              # 使用說明
```

### 使用範例

#### 情境1：朋友聚餐均攤

```text
用戶：#新增支出 600 聚餐 @小美 @小王 @小李
機器人：✅ 新增支出 B-1！
       名目: 聚餐
       付款人: @您 (您)
       總支出: 600.00
       類型: 均攤
       您的分攤: 150.00
       您實付: 600.00
       應收回: 450.00
       明細 (3人欠款):
       @小美 應付 150.00
       @小王 應付 150.00
       @小李 應付 150.00
```

#### 情境2：各自點餐不同價格

```text
用戶：#新增支出 1200 聚餐 @小美 350 @小王 280 @小李 320
機器人：✅ 新增支出 B-2！
       名目: 聚餐
       付款人: @您 (您)
       總支出: 1200.00
       類型: 分別計算
       您的分攤: 250.00
       您實付: 1200.00
       應收回: 950.00
       明細 (3人欠款):
       @小美 應付 350.00
       @小王 應付 280.00
       @小李 應付 320.00
```

#### 情境3：全面結算

```text
用戶：#全面結算
機器人：--- 💰 @您 全面結算報告 ---

       📊 統計摘要:
       • 總帳單數: 5 筆
       • 已結清: 3 筆
       • 部分付款: 1 筆
       • 完全未付: 1 筆
       • 自動封存: 3 筆

       💵 金額摘要:
       • 支出總額: 2500.00
       • 已收回: 1800.00
       • 待收回: 700.00

       ✨ 結算完成！已自動封存 3 筆結清帳單。
```

## 🔧 技術架構

### 系統架構

- **後端框架**：Flask + SQLAlchemy
- **資料庫**：PostgreSQL (支援SQLite開發)
- **LINE SDK**：line-bot-sdk
- **部署方式**：Docker + Heroku/Railway

### 資料庫設計

```sql
-- 群組成員表
sb_group_members (
    id, name, group_id, line_user_id, 
    created_at, updated_at
)

-- 帳單表
sb_bills (
    id, group_id, description, total_bill_amount,
    payer_member_id, split_type, content_hash,
    is_archived, created_at, updated_at
)

-- 帳單參與人表
sb_bill_participants (
    id, bill_id, debtor_member_id, amount_owed,
    is_paid, paid_at, created_at, updated_at
)

-- 重複操作防護表
sb_duplicate_prevention_log (
    id, operation_hash, group_id, user_id,
    operation_type, created_at
)
```

### 核心特性

1. **群組隔離**：複合唯一約束確保資料隔離
2. **重複防護**：Hash機制防止重複操作
3. **資料完整性**：外鍵約束和級聯刪除
4. **效能優化**：複合索引提升查詢效能

## 🛠️ 安裝與部署

### 環境需求

- Python 3.8+
- PostgreSQL 12+
- LINE Bot Channel

### LINE Bot 設定

1. 在 [LINE Developers Console](https://developers.line.biz/) 建立新的 Provider 和 Channel
2. 取得 Channel Access Token 和 Channel Secret
3. 設定 Webhook URL：`https://your-domain.com/splitbill/callback`
4. 啟用 Webhook 接收訊息

### 執行完整測試套件

```bash
python test_complete_v282.py
```

### 測試覆蓋範圍

- ✅ 資料庫結構測試
- ✅ 群組隔離功能
- ✅ 重複操作防護
- ✅ 均攤計算邏輯
- ✅ 分別計算邏輯
- ✅ 帳單建立和查詢
- ✅ 欠款追蹤
- ✅ 付款結算
- ✅ 帳單封存
- ✅ 內容hash防護
- ✅ 舊記錄清理

## 📚 API 文檔

### 主要端點

- `POST /splitbill/callback` - LINE Webhook 接收端點

### 核心功能函數

```python
# 分帳計算
parse_participant_input_v282(participants_str, total_amount, payer_name)

# 帳單管理
handle_add_bill_v280(reply_token, match, group_id, sender_line_user_id, sender_mention_name, db)
handle_list_bills_v280(reply_token, group_id, sender_line_user_id, db)
handle_bill_details_v280(reply_token, bill_db_id, group_id, sender_line_user_id, db)

# 結算功能
handle_settle_payment_v280(reply_token, bill_db_id, debtor_mentions_str, group_id, sender_line_user_id, db)
handle_complete_settlement_v282(reply_token, group_id, sender_line_user_id, db)
handle_archive_bill_v280(reply_token, bill_db_id, group_id, sender_line_user_id, db)
```

## 🔍 常見問題 FAQ

### Q: 為什麼不能@自己？

A: LINE平台不支援@自己的功能，但付款人會自動參與分攤計算，無需@自己。

### Q: 如何處理小數點問題？

A: 系統使用Decimal精確計算，尾數差額由付款人承擔。

### Q: 資料會在不同群組間混淆嗎？

A: 不會。系統有完善的群組隔離機制，確保資料安全。

### Q: 重複操作如何防護？

A: 系統使用雙重防護：操作hash防重複動作，內容hash防重複建立。

### Q: 如何清理舊資料？

A: 使用`#全面結算`自動封存已結清帳單，系統會自動清理舊的防護記錄。

## 🎯 版本更新歷程

### v2.8.2 (Latest)

- 🔥 新增全面結算功能
- 🎯 純粹的群組分帳計算邏輯
- 🚫 移除@自己相關的多餘程式碼
- 🛡️ 強化資料庫完整性和一致性

### v2.8.1

- 🧮 重新設計分攤邏輯
- 🎨 新增Flex Message界面
- ✨ 付款人預設參與分攤
- 🔧 優化用戶體驗

### v2.8.0

- 🛡️ 重複操作防護機制
- 🏠 群組隔離強化
- 📊 資料庫結構重新設計
- 🔒 內容hash防重複建立

## 🤝 貢獻指南

歡迎提交 Issue 和 Pull Request！

### 開發流程

1. Fork 本專案
2. 創建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 開啟 Pull Request

### 代碼風格

- 使用 Black 格式化代碼
- 遵循 PEP 8 規範
- 添加適當的類型註解
- 編寫完整的測試

## 📄 授權條款

本專案採用 MIT 授權條款 - 查看 [LICENSE](LICENSE) 檔案了解詳情。

## 🙏 致謝

感謝所有為本專案做出貢獻的開發者和使用者！

---
