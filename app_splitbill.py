# app_splitbill.py (v2.8.4 - Database-Level Duplicate Prevention)
from flask import Flask, request, abort, jsonify
import os
import re
from typing import List, Optional, Dict, Any, Set, Tuple
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
import logging

# 更新導入以包含新的原子性創建功能
from models_splitbill import (
    init_db_splitbill as init_db,
    get_db_splitbill as get_db,
    GroupMember, Bill, BillParticipant, SplitType, DuplicatePreventionLog,
    get_or_create_member_by_line_id, 
    get_or_create_member_by_name,    
    get_bill_by_id, get_active_bills_by_group,
    get_unpaid_debts_for_member_by_line_id,
    generate_content_hash_v284, generate_operation_hash,
    is_duplicate_operation, log_operation, cleanup_old_duplicate_logs,
    atomic_create_bill_v284
)
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from linebot import LineBotApi, WebhookHandler 
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    QuickReply, QuickReplyButton, MessageAction, PostbackAction
)

app = Flask(__name__)
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.error("LINE Channel Access Token/Secret未設定。")
    exit(1)

try:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    logger.info("LINE Bot API 初始化成功 (v2.8.4)。")
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

try:
    init_db()
    logger.info("分帳資料庫初始化檢查完成 (v2.8.4)。")
except Exception as e:
    logger.exception(f"分帳資料庫初始化失敗: {e}")

# --- Regex Patterns (v2.8.4) ---
ADD_BILL_PATTERN = r'^#新增支出\s+([\d\.]+)\s+(.+?)\s+((?:@\S+(?:\s+[\d\.]+)?\s*)+)$'
LIST_BILLS_PATTERN = r'^#帳單列表$'
BILL_DETAILS_PATTERN = r'^#支出詳情\s+B-(\d+)$'
SETTLE_PAYMENT_PATTERN = r'^#結帳\s+B-(\d+)\s+((?:@\S+\s*)+)$'
ARCHIVE_BILL_PATTERN = r'^#封存帳單\s+B-(\d+)$'
MY_DEBTS_PATTERN = r'^#我的欠款$'
HELP_PATTERN = r'^#幫助$'
# 新增Flex Message相關的指令
FLEX_CREATE_BILL_PATTERN = r'^#建立帳單$'
FLEX_MENU_PATTERN = r'^#選單$'
# 新增全面結算相關指令
COMPLETE_SETTLEMENT_PATTERN = r'^#全面結算$'
# v2.8.3 新增：群組總欠款查看
GROUP_DEBTS_OVERVIEW_PATTERN = r'^#群組欠款$'

def normalize_participants_string(participants_str: str) -> str:
    """標準化參與人字串用於生成一致的 content_hash - 已被 v2.8.4 更強的標準化取代"""
    # 提取所有 @提及 和金額組合
    mentions = re.findall(r'@(\S+)(?:\s+([\d\.]+))?', participants_str)
    
    # 按照用戶名稱排序以確保一致性
    sorted_mentions = sorted(mentions, key=lambda x: x[0])
    
    # 重新組合成標準格式
    normalized_parts = []
    for name, amount in sorted_mentions:
        if amount:
            normalized_parts.append(f"@{name} {amount}")
        else:
            normalized_parts.append(f"@{name}")
    
    return " ".join(normalized_parts)

def parse_participant_input_v282(participants_str: str, total_bill_amount_from_command: Decimal, payer_mention_name: str) \
        -> Tuple[Optional[List[Tuple[str, Decimal]]], Optional[SplitType], Optional[str], Decimal]:
    """
    v2.8.2 群組分帳計算邏輯：
    - 專注於群組成員間的債務計算
    - 付款人預設參與分攤但不會欠自己錢
    - 移除@自己的處理邏輯（LINE不支援）
    - 純粹的分帳計算工具
    """
    participants_to_charge: List[Tuple[str, Decimal]] = []
    error_msg = None
    split_type = None
    payer_share = Decimal(0)  # 付款人應分攤的金額

    # 解析@提及的參與人
    raw_mentions = re.findall(r'@(\S+)(?:\s+([\d\.]+))?', participants_str)

    if not raw_mentions:
        return None, None, "請至少 @提及一位參與的成員。", Decimal(0)

    has_any_amount_specified = any(amount_str for _, amount_str in raw_mentions)
    temp_name_set = set()
    other_participants = []  # 其他參與人（不包括付款人）

    # 收集參與人資訊，自動排除付款人
    for name, amount_str in raw_mentions:
        name = name.strip()
        if name in temp_name_set: 
            return None, None, f"參與人 @{name} 被重複提及。", Decimal(0)
        temp_name_set.add(name)
        
        # 自動排除付款人（避免自己欠自己錢）
        if name == payer_mention_name:
            logger.info(f"自動排除付款人自己({name})，避免自己欠自己錢")
            continue
            
        other_participants.append((name, amount_str))

    if not other_participants:
        return None, None, "請 @提及其他需要分攤的成員（付款人會自動參與分攤計算）。", Decimal(0)

    if has_any_amount_specified:
        # 分別計算模式：檢查是否有人指定了金額
        split_type = SplitType.UNEQUAL
        others_total = Decimal(0)
        
        # 計算其他參與人的指定金額
        for name, amount_str in other_participants:
            if not amount_str:
                return None, None, f"分別計算模式下，@{name} 未指定金額。請為所有參與人指定金額，或使用均攤模式。", Decimal(0)
            try:
                amount = Decimal(amount_str)
                if amount <= 0: 
                    return None, None, f"@{name} 的金額 ({amount_str}) 必須大於0。", Decimal(0)
                others_total += amount
                participants_to_charge.append((name, amount))
            except InvalidOperation:
                return None, None, f"@{name} 的金額 ({amount_str}) 格式無效。", Decimal(0)
        
        # 付款人負擔剩餘金額
        payer_share = total_bill_amount_from_command - others_total
        if payer_share <= 0:
            return None, None, f"其他人的指定金額總和 ({others_total}) 已超過總金額 ({total_bill_amount_from_command})，付款人無法負擔剩餘金額。", Decimal(0)
            
    else:
        # 均攤模式：付款人 + 其他參與人平均分攤
        split_type = SplitType.EQUAL
        total_participants = len(other_participants) + 1  # +1 包括付款人
        
        # 計算每人應負擔的金額
        individual_share_raw = total_bill_amount_from_command / Decimal(total_participants)
        individual_share = individual_share_raw.quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')
        
        # 處理尾數問題：讓付款人承擔尾數差額
        others_total = individual_share * Decimal(len(other_participants))
        payer_share = total_bill_amount_from_command - others_total
        
        # 為其他參與人分配金額
        for name, _ in other_participants:
            participants_to_charge.append((name, individual_share))

    return participants_to_charge, split_type, error_msg, payer_share

@app.route("/splitbill/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    # logger.debug(f"分帳Bot Request body: {body}") # Keep for debugging if needed
    try:
        handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    except Exception as e: logger.exception(f"處理分帳Bot回調錯誤: {e}"); abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    text = event.message.text.strip()
    reply_token = event.reply_token

    if not reply_token or reply_token == "<no-reply>":
        logger.warning(f"分帳Bot: Invalid reply_token. Source: {event.source}")
        return

    source = event.source
    group_id: Optional[str] = None
    sender_line_user_id: str = source.user_id 

    if source.type == 'group': group_id = source.group_id
    elif source.type == 'room': group_id = source.room_id
    else:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="此分帳機器人僅限群組內使用。"))
        return

    logger.info(f"分帳Bot Received from G/R ID {group_id} by UserLINEID {sender_line_user_id}: '{text}'")

    # 獲取發送者在群組中的顯示名稱
    sender_mention_name = ""
    try:
        profile = line_bot_api.get_group_member_profile(group_id, sender_line_user_id)
        sender_mention_name = profile.display_name
    except LineBotApiError as e_profile:
        logger.warning(f"無法獲取發送者 (LINEID:{sender_line_user_id}) 在群組 {group_id} 的 Profile: {e_profile.status_code}")

    try:
        with get_db() as db:
            # 定期清理舊的重複操作記錄（每100次操作清理一次）
            if hash(text) % 100 == 0:
                cleanup_old_duplicate_logs(db)
                db.commit()

            add_bill_match = re.match(ADD_BILL_PATTERN, text)
            list_bills_match = re.match(LIST_BILLS_PATTERN, text)
            bill_details_match = re.match(BILL_DETAILS_PATTERN, text)
            settle_payment_match = re.match(SETTLE_PAYMENT_PATTERN, text)
            archive_bill_match = re.match(ARCHIVE_BILL_PATTERN, text)
            my_debts_match = re.match(MY_DEBTS_PATTERN, text)
            help_match = re.match(HELP_PATTERN, text)
            flex_create_bill_match = re.match(FLEX_CREATE_BILL_PATTERN, text)
            flex_menu_match = re.match(FLEX_MENU_PATTERN, text)
            complete_settlement_match = re.match(COMPLETE_SETTLEMENT_PATTERN, text)
            group_debts_overview_match = re.match(GROUP_DEBTS_OVERVIEW_PATTERN, text)

            if add_bill_match:
                if not sender_mention_name:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="無法獲取您的群組名稱，請稍後再試。"))
                    return
                handle_add_bill_v284(reply_token, add_bill_match, group_id, sender_line_user_id, sender_mention_name, db)
            elif list_bills_match:
                handle_list_bills_v280(reply_token, group_id, sender_line_user_id, db)
            elif bill_details_match:
                bill_db_id = int(bill_details_match.group(1))
                handle_bill_details_v280(reply_token, bill_db_id, group_id, sender_line_user_id, db)
            elif settle_payment_match:
                bill_db_id = int(settle_payment_match.group(1))
                debtor_mentions_str = settle_payment_match.group(2)
                handle_settle_payment_v280(reply_token, bill_db_id, debtor_mentions_str, group_id, sender_line_user_id, db)
            elif archive_bill_match:
                bill_db_id = int(archive_bill_match.group(1))
                handle_archive_bill_v280(reply_token, bill_db_id, group_id, sender_line_user_id, db)
            elif my_debts_match:
                handle_my_debts_v280(reply_token, sender_line_user_id, group_id, db)
            elif help_match:
                send_splitbill_help_v284(reply_token)
            elif flex_create_bill_match:
                send_flex_create_bill_menu_v280(reply_token)
            elif flex_menu_match:
                send_flex_main_menu_v284(reply_token)
            elif complete_settlement_match:
                handle_complete_settlement_v283(reply_token, group_id, sender_line_user_id, db)
            elif group_debts_overview_match:
                handle_group_debts_overview_v283(reply_token, group_id, sender_line_user_id, db)
            else:
                logger.info(f"分帳Bot: Unmatched command '{text}' in group {group_id}")

    except SQLAlchemyError as db_err:
        logger.exception(f"分帳Bot DB錯誤: {db_err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="資料庫操作錯誤，請稍後再試。"))
    except InvalidOperation as dec_err:
        logger.warning(f"分帳Bot Decimal轉換錯誤: {dec_err} for text: {text}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"金額格式錯誤: {dec_err}"))
    except ValueError as val_err:
        logger.warning(f"分帳Bot 數值或格式錯誤: {val_err} for text: {text}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"輸入錯誤: {val_err}"))
    except LineBotApiError as line_err:
        logger.error(f"分帳Bot LINE API 錯誤: Status={line_err.status_code}, Message={line_err.error.message if line_err.error else 'N/A'}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"與LINE平台溝通時發生錯誤 ({line_err.status_code})。"))
    except Exception as e:
        logger.exception(f"分帳Bot 未預期錯誤: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="發生未預期錯誤，請稍後再試。"))

def handle_add_bill_v284(reply_token: str, match: re.Match, group_id: str, payer_line_user_id: str, payer_mention_name: str, db: Session):
    """
    新增帳單功能 v2.8.4 - 使用資料庫約束徹底防止重複：
    - 資料庫層面唯一約束
    - 原子性事務處理
    - 強化的內容hash生成
    - 優雅的重複處理
    """
    total_amount_str = match.group(1)
    description = match.group(2).strip()
    participants_input_str = match.group(3).strip()

    logger.info(f"處理新增帳單請求 - 用戶: {payer_line_user_id}, 群組: {group_id}, 描述: {description}")

    if not description:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請提供支出說明。"))
        return
        
    try:
        total_bill_amount = Decimal(total_amount_str)
        if total_bill_amount <= 0: 
            raise ValueError("總支出金額必須大於0。")
    except (InvalidOperation, ValueError) as e:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"總支出金額 '{total_amount_str}' 無效: {e}"))
        return

    # 確保付款人存在於該群組中
    payer_member_obj = get_or_create_member_by_line_id(db, line_user_id=payer_line_user_id, group_id=group_id, display_name=payer_mention_name)

    # 解析參與人
    participants_to_charge_data, split_type, error_msg, payer_share = \
        parse_participant_input_v282(participants_input_str, total_bill_amount, payer_mention_name)

    if error_msg:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"參與人解析錯誤: {error_msg}"))
        return

    # 生成強化版內容hash
    content_hash = generate_content_hash_v284(
        payer_id=payer_member_obj.id,
        description=description,
        amount=total_amount_str,
        participants_str=participants_input_str,
        group_id=group_id
    )

    # 準備帳單資料
    bill_data = {
        'group_id': group_id,
        'description': description,
        'total_bill_amount': total_bill_amount,
        'payer_member_id': payer_member_obj.id,
        'split_type': split_type,
        'content_hash': content_hash
    }

    # 準備參與人資料
    participants_data = []
    for p_name, p_amount_owed in participants_to_charge_data:
        debtor_member_obj = get_or_create_member_by_name(db, name=p_name, group_id=group_id)
        participants_data.append({
            'debtor_member_id': debtor_member_obj.id,
            'amount_owed': p_amount_owed,
            'is_paid': False
        })

    # 使用原子性創建帳單
    result_bill, status = atomic_create_bill_v284(db, bill_data, participants_data)

    # 處理不同的創建結果
    if status == "success":
        # 成功創建新帳單
        participant_details_msg = [f"@{p_bp.debtor_member_profile.name} 應付 {p_bp.amount_owed:.2f}" for p_bp in result_bill.participants]
        
        # 計算其他人應付的總額
        others_total = sum(bp.amount_owed for bp in result_bill.participants)
        
        reply_msg = (
            f"✅ 新增支出 B-{result_bill.id}！\n名目: {result_bill.description}\n"
            f"付款人: @{result_bill.payer_member_profile.name} (您)\n"
            f"總支出: {result_bill.total_bill_amount:.2f}\n"
            f"類型: {'均攤' if result_bill.split_type == SplitType.EQUAL else '分別計算'}\n"
        )
        
        if payer_share and payer_share > 0:
            reply_msg += f"您的分攤: {payer_share:.2f}\n"
            reply_msg += f"您實付: {result_bill.total_bill_amount:.2f}\n"
            reply_msg += f"應收回: {others_total:.2f}\n"
        
        if participant_details_msg:
            reply_msg += f"明細 ({len(participant_details_msg)}人欠款):\n" + "\n".join(participant_details_msg)
        else:
            reply_msg += "  (此筆支出無其他人需向您付款)"
        reply_msg += f"\n\n查閱: #支出詳情 B-{result_bill.id}"
        
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg))
        logger.info(f"成功新增帳單 B-{result_bill.id} - 群組: {group_id}, 付款人: {payer_line_user_id}")

    elif status in ["duplicate_found", "duplicate_constraint"]:
        # 發現重複帳單
        if result_bill:
            reply_msg = (
                f"⚠️ 相同內容的帳單已存在！\n"
                f"帳單 B-{result_bill.id}: {result_bill.description}\n"
                f"金額: {result_bill.total_bill_amount:.2f}\n"
                f"建立時間: {result_bill.created_at.strftime('%m/%d %H:%M') if result_bill.created_at else 'N/A'}\n\n"
                f"如需查看詳情請使用: #支出詳情 B-{result_bill.id}"
            )
        else:
            reply_msg = "⚠️ 偵測到重複的帳單內容，請稍候再試或修改帳單內容。"
        
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg))
        logger.warning(f"阻止重複帳單創建 - 群組: {group_id}, 付款人: {payer_line_user_id}, Hash: {content_hash}")

    else:
        # 其他錯誤
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出時發生錯誤，請稍後再試。"))
        logger.error(f"新增帳單失敗 - 狀態: {status}, 群組: {group_id}, 付款人: {payer_line_user_id}")

def handle_list_bills_v280(reply_token: str, group_id: str, sender_line_user_id: str, db: Session):
    """列出帳單功能，加入重複操作防護"""
    operation_hash = generate_operation_hash(sender_line_user_id, "list_bills", group_id)

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=1):
        return  # 靜默忽略重複的列表請求

    log_operation(db, operation_hash, group_id, sender_line_user_id, "list_bills")

    bills = get_active_bills_by_group(db, group_id)
    if not bills: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text="目前無待處理帳單。"))
        return
        
    reply_items = [f"--- 📜 本群組帳單列表 (未封存) ---"]
    for bill in bills:
        item = (f"\nID: B-{bill.id} | {bill.description}\n"
                f"付款人: @{bill.payer_member_profile.name} | 總額: {bill.total_bill_amount:.2f}\n")
        all_paid_for_bill = True if bill.participants else False
        if not bill.participants: item += "  (尚無參與人)"
        else:
            for p in bill.participants:
                item += f"\n  @{p.debtor_member_profile.name}: {p.amount_owed:.2f} ({'✅已付' if p.is_paid else '🅾️未付'})"
                if not p.is_paid: all_paid_for_bill = False
        if all_paid_for_bill and bill.participants: item += "\n✨ 此帳單已結清！"
        item += f"\n(詳情: #支出詳情 B-{bill.id})"
        reply_items.append(item)
    full_reply = "\n".join(reply_items)
    line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply[:4950] + ("..." if len(full_reply)>4950 else "")))

def handle_bill_details_v280(reply_token: str, bill_db_id: int, group_id: str, sender_line_user_id: str, db: Session):
    """帳單詳情功能，確保群組隔離"""
    operation_hash = generate_operation_hash(sender_line_user_id, "bill_details", f"{group_id}:{bill_db_id}")

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=1):
        return  # 靜默忽略重複的詳情請求

    log_operation(db, operation_hash, group_id, sender_line_user_id, "bill_details")

    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。"))
        return
        
    paid_count = sum(1 for p in bill.participants if p.is_paid)
    total_participants = len(bill.participants)
    reply_msg = (
        f"--- 💳 支出詳情: B-{bill.id} ---\n"
        f"名目: {bill.description}\n付款人: @{bill.payer_member_profile.name}\n總額: {bill.total_bill_amount:.2f}\n"
        f"類型: {'均攤' if bill.split_type == SplitType.EQUAL else '分別計算'}\n"
        f"建立於: {bill.created_at.strftime('%y/%m/%d %H:%M') if bill.created_at else 'N/A'}\n"
        f"狀態: {'已封存' if bill.is_archived else '處理中'}\n"
        f"進度: {paid_count}/{total_participants} 人已付\n參與人:"
    )
    if not bill.participants: reply_msg += "\n  (無參與人)"
    else:
        for p in bill.participants:
            reply_msg += f"\n  {'✅' if p.is_paid else '🅾️'} @{p.debtor_member_profile.name} 應付 {p.amount_owed:.2f} " + (f"({p.paid_at.strftime('%y/%m/%d')})" if p.is_paid and p.paid_at else "")
    if not bill.is_archived:
        reply_msg += f"\n\n➡️ 付款人可 `#結帳 B-{bill.id} @已付成員` 更新。"
        if bill.participants and paid_count == total_participants:
            reply_msg += f"\n➡️ 已結清！付款人可 `#封存帳單 B-{bill.id}`。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg[:4950] + ("..." if len(reply_msg)>4950 else "")))

def handle_settle_payment_v280(reply_token: str, bill_db_id: int, debtor_mentions_str: str, group_id: str, sender_line_user_id: str, db: Session):
    """結帳功能，加入重複操作防護和權限驗證"""
    operation_content = f"settle:{bill_db_id}:{debtor_mentions_str}"
    operation_hash = generate_operation_hash(sender_line_user_id, "settle_payment", operation_content)

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=1):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 偵測到重複結帳操作，請稍等片刻再試。"))
        return

    log_operation(db, operation_hash, group_id, sender_line_user_id, "settle_payment")

    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。"))
        return

    # 驗證操作權限：只有付款人才能執行結帳
    if not bill.payer_member_profile.line_user_id or bill.payer_member_profile.line_user_id != sender_line_user_id:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"只有此帳單的付款人 @{bill.payer_member_profile.name} 才能執行結帳。"))
        return
        
    if bill.is_archived: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 已封存。"))
        return

    debtor_names_to_settle = {name.strip() for name in re.findall(r'@(\S+)', debtor_mentions_str) if name.strip()}
    if not debtor_names_to_settle: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請 @提及 至少一位要標記付款的參與人。"))
        return

    settled_count, already_paid_names, not_found_names = 0, [], list(debtor_names_to_settle)
    for bp in bill.participants:
        if bp.debtor_member_profile.name in debtor_names_to_settle:
            if bp.debtor_member_profile.name in not_found_names: 
                not_found_names.remove(bp.debtor_member_profile.name)
            if not bp.is_paid: 
                bp.is_paid = True
                bp.paid_at = datetime.now(timezone.utc)
                bp.updated_at = datetime.now(timezone.utc)
                settled_count += 1
            else: 
                already_paid_names.append(f"@{bp.debtor_member_profile.name}")

    if settled_count > 0: 
        bill.updated_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"成功結帳 {settled_count} 人 - 帳單: B-{bill_db_id}, 群組: {group_id}")

    reply_parts = []
    if settled_count > 0: reply_parts.append(f"✅ 已為 B-{bill_db_id} 標記 {settled_count} 人付款。")
    if already_paid_names: reply_parts.append(f"提示: {', '.join(already_paid_names)} 先前已付。")
    if not_found_names: reply_parts.append(f"注意: 於此帳單找不到參與人: {', '.join(['@'+n for n in not_found_names])}。")
    line_bot_api.reply_message(reply_token, TextSendMessage(text="\n".join(reply_parts) if reply_parts else "無效操作或未提及有效參與人。"))

def handle_archive_bill_v280(reply_token: str, bill_db_id: int, group_id: str, sender_line_user_id: str, db: Session):
    """封存帳單功能，加入重複操作防護和權限驗證"""
    operation_hash = generate_operation_hash(sender_line_user_id, "archive_bill", f"{group_id}:{bill_db_id}")

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=1):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 偵測到重複封存操作，請稍等片刻再試。"))
        return

    log_operation(db, operation_hash, group_id, sender_line_user_id, "archive_bill")

    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。"))
        return

    # 驗證操作權限：只有付款人才能執行封存
    if not bill.payer_member_profile.line_user_id or bill.payer_member_profile.line_user_id != sender_line_user_id:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"只有此帳單的付款人 @{bill.payer_member_profile.name} 才能執行封存。"))
        return
        
    if bill.is_archived: 
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 已封存。"))
        return

    bill.is_archived = True
    bill.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ 帳單 B-{bill_db_id} ({bill.description[:20]}...) 已封存。"))
        logger.info(f"成功封存帳單 B-{bill_db_id} - 群組: {group_id}, 操作者: {sender_line_user_id}")
    except Exception as e: 
        db.rollback()
        logger.error(f"封存帳單 B-{bill_db_id} 失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="封存失敗。"))

def handle_my_debts_v280(reply_token: str, sender_line_user_id: str, group_id: str, db: Session):
    """我的欠款功能，確保群組隔離"""
    operation_hash = generate_operation_hash(sender_line_user_id, "my_debts", group_id)

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=1):
        return  # 靜默忽略重複的欠款查詢

    log_operation(db, operation_hash, group_id, sender_line_user_id, "my_debts")

    unpaid_participations = get_unpaid_debts_for_member_by_line_id(db, sender_line_user_id, group_id)

    sender_display_name_for_msg = "您"
    try:
        profile = line_bot_api.get_group_member_profile(group_id, sender_line_user_id)
        sender_display_name_for_msg = f"@{profile.display_name}"
    except Exception: 
        logger.warning(f"無法獲取 {sender_line_user_id} 在群組 {group_id} 的名稱用於 #我的欠款 回覆。")

    if not unpaid_participations:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"{sender_display_name_for_msg}目前在本群組無未付款項！🎉"))
        return

    reply_items = [f"--- 💸 {sender_display_name_for_msg} 的未付款項 ---"]
    total_owed_all_bills = Decimal(0)
    for bp in unpaid_participations:
        reply_items.append(f"\n帳單 B-{bp.bill.id}: {bp.bill.description}\n  應付 @{bp.bill.payer_member_profile.name}: {bp.amount_owed:.2f}\n  (詳情: #支出詳情 B-{bp.bill.id})")
        total_owed_all_bills += bp.amount_owed
    reply_items.append(f"\n--------------------\n欠款總額: {total_owed_all_bills:.2f}")
    full_reply = "\n".join(reply_items)
    line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply[:4950] + ("..." if len(full_reply)>4950 else "")))

def handle_complete_settlement_v283(reply_token: str, group_id: str, sender_line_user_id: str, db: Session):
    """
    全面結算功能 v2.8.3：
    - 處理付款人的所有相關帳單
    - 自動封存已結清的帳單
    - 提供完整的結算報告
    - 確保資料庫狀態一致性
    - 修復封存邏輯問題
    """
    operation_hash = generate_operation_hash(sender_line_user_id, "complete_settlement", group_id)

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=2):
        line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 偵測到重複結算操作，請稍等片刻再試。"))
        return

    log_operation(db, operation_hash, group_id, sender_line_user_id, "complete_settlement")

    # 獲取發送者資訊
    sender_display_name = "您"
    try:
        profile = line_bot_api.get_group_member_profile(group_id, sender_line_user_id)
        sender_display_name = f"@{profile.display_name}"
    except Exception: 
        logger.warning(f"無法獲取 {sender_line_user_id} 在群組 {group_id} 的名稱。")

    # 查找發送者在該群組中的成員記錄
    payer_member = db.query(GroupMember).filter(
        GroupMember.line_user_id == sender_line_user_id,
        GroupMember.group_id == group_id
    ).first()

    if not payer_member:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="找不到您在本群組的成員記錄，請先建立一筆帳單。"))
        return

    # 獲取所有由該成員付款且未封存的帳單
    payer_bills = db.query(Bill).options(
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
    ).filter(
        Bill.payer_member_id == payer_member.id,
        Bill.group_id == group_id,
        Bill.is_archived == False
    ).order_by(Bill.created_at.asc()).all()

    if not payer_bills:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"{sender_display_name} 目前沒有需要結算的帳單。"))
        return

    # 開始結算處理
    settlement_summary = {
        'total_bills': len(payer_bills),
        'fully_paid_bills': 0,
        'partially_paid_bills': 0,
        'unpaid_bills': 0,
        'archived_bills': 0,
        'total_amount': Decimal(0),
        'total_received': Decimal(0),
        'total_pending': Decimal(0)
    }

    settlement_details = []

    try:
        for bill in payer_bills:
            bill_total = bill.total_bill_amount
            bill_received = Decimal(0)
            bill_pending = Decimal(0)
            paid_count = 0
            total_participants = len(bill.participants)

            # 統計每筆帳單的付款狀況
            for participant in bill.participants:
                if participant.is_paid:
                    bill_received += participant.amount_owed
                    paid_count += 1
                else:
                    bill_pending += participant.amount_owed

            settlement_summary['total_amount'] += bill_total
            settlement_summary['total_received'] += bill_received
            settlement_summary['total_pending'] += bill_pending

            # 詳細記錄帳單狀態供除錯
            logger.info(f"結算檢查 B-{bill.id}: 參與人={total_participants}, 已付={paid_count}, 描述={bill.description}")
            for p in bill.participants:
                logger.info(f"  參與人 @{p.debtor_member_profile.name}: 金額={p.amount_owed}, 已付={p.is_paid}")

            # 分類帳單狀態
            if total_participants == 0:
                # 無參與人的帳單（只有付款人）
                settlement_summary['fully_paid_bills'] += 1
                settlement_details.append(f"B-{bill.id}: {bill.description[:15]}... (無其他參與人)")
                
                # 自動封存無參與人的帳單
                bill.is_archived = True
                bill.updated_at = datetime.now(timezone.utc)
                settlement_summary['archived_bills'] += 1
                logger.info(f"封存帳單 B-{bill.id} (無參與人)")
                
            elif paid_count == total_participants and total_participants > 0:
                # 已全部付清且有參與人
                settlement_summary['fully_paid_bills'] += 1
                settlement_details.append(f"✅ B-{bill.id}: {bill.description[:15]}... (已結清 {bill_received:.2f})")
                
                # 自動封存已結清的帳單
                bill.is_archived = True
                bill.updated_at = datetime.now(timezone.utc)
                settlement_summary['archived_bills'] += 1
                logger.info(f"封存帳單 B-{bill.id} (已結清: {paid_count}/{total_participants})")
                
            elif paid_count > 0:
                # 部分付清
                settlement_summary['partially_paid_bills'] += 1
                settlement_details.append(f"🔄 B-{bill.id}: {bill.description[:15]}... (已收 {bill_received:.2f}/剩餘 {bill_pending:.2f})")
                logger.info(f"部分結清 B-{bill.id}: {paid_count}/{total_participants}")
                
            else:
                # 完全未付
                settlement_summary['unpaid_bills'] += 1
                settlement_details.append(f"⭕ B-{bill.id}: {bill.description[:15]}... (待收 {bill_pending:.2f})")
                logger.info(f"完全未付 B-{bill.id}: {paid_count}/{total_participants}")

        # 提交所有更改
        db.commit()
        
        # 生成結算報告
        report_lines = [
            f"--- 💰 {sender_display_name} 全面結算報告 ---",
            f"",
            f"📊 統計摘要:",
            f"• 總帳單數: {settlement_summary['total_bills']} 筆",
            f"• 已結清: {settlement_summary['fully_paid_bills']} 筆",
            f"• 部分付款: {settlement_summary['partially_paid_bills']} 筆", 
            f"• 完全未付: {settlement_summary['unpaid_bills']} 筆",
            f"• 自動封存: {settlement_summary['archived_bills']} 筆",
            f"",
            f"💵 金額摘要:",
            f"• 支出總額: {settlement_summary['total_amount']:.2f}",
            f"• 已收回: {settlement_summary['total_received']:.2f}",
            f"• 待收回: {settlement_summary['total_pending']:.2f}",
            f"",
            f"📋 帳單詳情:"
        ]
        
        # 添加帳單詳情
        for detail in settlement_details[:10]:  # 限制顯示數量避免訊息過長
            report_lines.append(f"  {detail}")
            
        if len(settlement_details) > 10:
            report_lines.append(f"  ... 以及其他 {len(settlement_details) - 10} 筆帳單")

        report_lines.extend([
            f"",
            f"✨ 結算完成！已自動封存 {settlement_summary['archived_bills']} 筆結清帳單。",
            f"📱 使用 #帳單列表 查看剩餘待處理帳單。"
        ])

        full_report = "\n".join(report_lines)
        line_bot_api.reply_message(reply_token, TextSendMessage(text=full_report[:4950] + ("..." if len(full_report)>4950 else "")))
        
        logger.info(f"完成全面結算 - 用戶: {sender_line_user_id}, 群組: {group_id}, 處理帳單: {settlement_summary['total_bills']} 筆, 封存: {settlement_summary['archived_bills']} 筆")

    except Exception as e:
        db.rollback()
        logger.exception(f"全面結算時發生錯誤 - 用戶: {sender_line_user_id}, 群組: {group_id}: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="結算過程中發生錯誤，請稍後再試。"))

def send_splitbill_help_v284(reply_token: str):
    """v2.8.4 更新的幫助訊息"""
    help_text = (
        "--- 💸 分帳機器人指令 (v2.8.4) --- \n\n"
        "🔸 新增支出 (您自動參與分攤):\n"
        "  `#新增支出 <總金額> <說明> @參與人A @參與人B...` (均攤)\n"
        "    例: `#新增支出 300 午餐 @小美 @小王`\n"
        "    → 您和2位朋友均攤，每人100元\n\n"
        "  `#新增支出 <總金額> <說明> @參與人A <金額A> @參與人B <金額B>...` (分別計算)\n"
        "    例: `#新增支出 1000 聚餐 @小美 400 @小王 350`\n"
        "    → 您負擔剩餘250元，小美400元，小王350元\n\n"
        "💡 重要：\n"
        "  • 您作為付款人會自動參與分攤\n"
        "  • 不需要@自己（LINE不支援）\n"
        "  • 專注於群組成員間的債務計算\n\n"
        "🔸 視覺化選單:\n  `#選單` - 主選單\n  `#建立帳單` - 帳單建立精靈\n\n"
        "🔸 查看功能:\n  `#帳單列表` - 查看所有帳單\n  `#支出詳情 B-ID` - 查看特定帳單\n  `#我的欠款` - 查看個人未付款項\n  `#群組欠款` - 查看群組所有成員欠款總覽\n\n"
        "🔸 結算功能:\n  `#結帳 B-ID @已付成員1 @成員2...` - 標記個別付款\n  `#全面結算` - 智慧結算所有帳單並自動封存\n  `#封存帳單 B-ID` - 手動封存帳單\n\n"
        "🔸 本說明:\n  `#幫助`\n\n"
        "🚀 v2.8.4 重大更新:\n"
        "- 🔒 資料庫層面唯一約束：徹底防止重複帳單\n"
        "- ⚡ 原子性事務處理：確保資料一致性\n"
        "- 🧮 強化內容hash：智慧標準化所有輸入\n"
        "- 🛡️ 競爭條件保護：解決併發請求問題\n"
        "- 🎯 根本性解決方案：不再依賴時間窗口"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

def send_flex_main_menu_v284(reply_token: str):
    """發送主選單Flex Message v2.8.4"""
    flex_message = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "💸 分帳機器人",
                    "weight": "bold",
                    "size": "xl",
                    "color": "#2E7D32"
                },
                {
                    "type": "text",
                    "text": "v2.8.4",
                    "size": "sm",
                    "color": "#666666"
                }
            ],
            "paddingAll": "20px",
            "backgroundColor": "#E8F5E8"
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "選擇您要使用的功能：",
                    "size": "md",
                    "margin": "md"
                },
                {
                    "type": "separator",
                    "margin": "lg"
                }
            ],
            "paddingAll": "20px"
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "🆕 建立帳單",
                        "text": "#建立帳單"
                    },
                    "color": "#4CAF50"
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "📋 查看帳單列表",
                        "text": "#帳單列表"
                    }
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "💰 我的欠款",
                                "text": "#我的欠款"
                            },
                            "flex": 1
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "👥 群組欠款",
                                "text": "#群組欠款"
                            },
                            "flex": 1
                        }
                    ]
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "🎯 全面結算",
                        "text": "#全面結算"
                    }
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "message",
                        "label": "❓ 使用說明",
                        "text": "#幫助"
                    }
                }
            ],
            "paddingAll": "20px"
        }
    }
    
    line_bot_api.reply_message(
        reply_token,
        FlexSendMessage(alt_text="分帳機器人主選單", contents=flex_message)
    )

def send_flex_create_bill_menu_v280(reply_token: str):
    """發送建立帳單選單Flex Message"""
    flex_message = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "🆕 建立新帳單",
                    "weight": "bold",
                    "size": "xl",
                    "color": "#2E7D32"
                },
                {
                    "type": "text",
                    "text": "選擇分帳方式",
                    "size": "sm",
                    "color": "#666666"
                }
            ],
            "paddingAll": "20px",
            "backgroundColor": "#E8F5E8"
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": "📌 均攤模式",
                            "weight": "bold",
                            "size": "md",
                            "color": "#2E7D32"
                        },
                        {
                            "type": "text",
                            "text": "所有人平均分攤費用",
                            "size": "sm",
                            "color": "#666666",
                            "margin": "xs"
                        },
                        {
                            "type": "text",
                            "text": "範例: 午餐 300元，3人分攤",
                            "size": "xs",
                            "color": "#999999",
                            "margin": "xs"
                        }
                    ],
                    "backgroundColor": "#F5F5F5",
                    "paddingAll": "15px",
                    "cornerRadius": "8px",
                    "margin": "md"
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": "🎯 分別計算模式",
                            "weight": "bold",
                            "size": "md",
                            "color": "#FF9800"
                        },
                        {
                            "type": "text",
                            "text": "每人負擔不同金額",
                            "size": "sm",
                            "color": "#666666",
                            "margin": "xs"
                        },
                        {
                            "type": "text",
                            "text": "範例: 點餐各自不同價格",
                            "size": "xs",
                            "color": "#999999",
                            "margin": "xs"
                        }
                    ],
                    "backgroundColor": "#FFF8E1",
                    "paddingAll": "15px",
                    "cornerRadius": "8px",
                    "margin": "md"
                }
            ],
            "paddingAll": "20px"
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": "📝 指令格式：",
                    "weight": "bold",
                    "size": "sm",
                    "margin": "md"
                },
                {
                    "type": "text",
                    "text": "均攤：#新增支出 300 午餐 @小美 @小王",
                    "size": "xs",
                    "color": "#666666",
                    "wrap": True,
                    "margin": "xs"
                },
                {
                    "type": "text",
                    "text": "分別：#新增支出 1000 聚餐 @小美 400 @小王 350",
                    "size": "xs",
                    "color": "#666666",
                    "wrap": True,
                    "margin": "xs"
                },
                {
                    "type": "text",
                    "text": "💡 您會自動參與分攤，無需@自己",
                    "size": "xs",
                    "color": "#FF9800",
                    "wrap": True,
                    "margin": "sm"
                },
                {
                    "type": "separator",
                    "margin": "lg"
                },
                {
                    "type": "box",
                    "layout": "horizontal",
                    "spacing": "sm",
                    "contents": [
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "🔙 返回選單",
                                "text": "#選單"
                            },
                            "flex": 1
                        },
                        {
                            "type": "button",
                            "style": "secondary",
                            "height": "sm",
                            "action": {
                                "type": "message",
                                "label": "❓ 詳細說明",
                                "text": "#幫助"
                            },
                            "flex": 1
                        }
                    ]
                }
            ],
            "paddingAll": "20px"
        }
    }
    
    line_bot_api.reply_message(
        reply_token,
        FlexSendMessage(alt_text="建立帳單選單", contents=flex_message)
    )

def handle_group_debts_overview_v283(reply_token: str, group_id: str, sender_line_user_id: str, db: Session):
    """群組總欠款查看功能 - 顯示群組中所有成員的欠款狀況"""
    operation_hash = generate_operation_hash(sender_line_user_id, "group_debts_overview", group_id)

    if is_duplicate_operation(db, operation_hash, group_id, sender_line_user_id, time_window_minutes=1):
        return  # 靜默忽略重複的群組欠款查詢

    log_operation(db, operation_hash, group_id, sender_line_user_id, "group_debts_overview")

    # 查詢群組中所有未付款的債務記錄
    all_unpaid_participations = db.query(BillParticipant).options(
        joinedload(BillParticipant.debtor_member_profile),
        joinedload(BillParticipant.bill).joinedload(Bill.payer_member_profile)
    ).join(Bill).filter(
        Bill.group_id == group_id,
        Bill.is_archived == False,
        BillParticipant.is_paid == False
    ).order_by(BillParticipant.debtor_member_id, Bill.created_at).all()

    if not all_unpaid_participations:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="🎉 群組內目前無任何未結清欠款！"))
        return

    # 按債務人整理欠款資訊
    debts_by_member = {}
    total_group_debt = Decimal(0)
    
    for participation in all_unpaid_participations:
        debtor_name = participation.debtor_member_profile.name
        if debtor_name not in debts_by_member:
            debts_by_member[debtor_name] = {
                'total_owed': Decimal(0),
                'bills': []
            }
        
        debts_by_member[debtor_name]['total_owed'] += participation.amount_owed
        debts_by_member[debtor_name]['bills'].append({
            'bill_id': participation.bill.id,
            'description': participation.bill.description,
            'amount_owed': participation.amount_owed,
            'payer_name': participation.bill.payer_member_profile.name
        })
        total_group_debt += participation.amount_owed

    # 生成報告
    reply_lines = [
        "--- 💰 群組欠款總覽 ---",
        f"",
        f"📊 總計摘要:",
        f"• 欠款成員: {len(debts_by_member)} 人",
        f"• 群組總欠款: {total_group_debt:.2f}",
        f"",
        f"📋 欠款詳情:"
    ]

    # 按欠款金額排序（從高到低）
    sorted_debtors = sorted(debts_by_member.items(), key=lambda x: x[1]['total_owed'], reverse=True)
    
    for debtor_name, debt_info in sorted_debtors[:8]:  # 限制顯示前8名避免訊息過長
        reply_lines.append(f"")
        reply_lines.append(f"👤 @{debtor_name}")
        reply_lines.append(f"  欠款總額: {debt_info['total_owed']:.2f}")
        
        # 顯示該成員的前3筆欠款
        for bill_info in debt_info['bills'][:3]:
            reply_lines.append(f"  • B-{bill_info['bill_id']}: {bill_info['description'][:12]}...")
            reply_lines.append(f"    欠 @{bill_info['payer_name']}: {bill_info['amount_owed']:.2f}")
        
        if len(debt_info['bills']) > 3:
            reply_lines.append(f"    ... 及其他 {len(debt_info['bills']) - 3} 筆欠款")

    if len(sorted_debtors) > 8:
        reply_lines.append(f"")
        reply_lines.append(f"... 及其他 {len(sorted_debtors) - 8} 位成員")

    reply_lines.extend([
        f"",
        f"💡 提示:",
        f"• 使用 #我的欠款 查看個人詳細欠款",
        f"• 使用 #帳單列表 查看所有帳單"
    ])

    full_reply = "\n".join(reply_lines)
    line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply[:4950] + ("..." if len(full_reply)>4950 else "")))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 7777)) 
    host = '0.0.0.0'
    logger.info(f"分帳Bot Flask 應用 (開發伺服器 v2.8.4) 啟動於 host={host}, port={port}")
    try:
        app.run(host=host, port=port, debug=True) 
    except Exception as e:
        logger.exception(f"啟動分帳Bot Flask 應用 (開發伺服器) 時發生錯誤: {e}")