# app_splitbill.py (v2.8.0 - Fixed Group Isolation & Duplicate Prevention)
from flask import Flask, request, abort, jsonify
import os
import re
from typing import List, Optional, Dict, Any, Set, Tuple
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
import logging

# 更新導入以包含新的重複操作防護功能
from models_splitbill import (
    init_db_splitbill as init_db,
    get_db_splitbill as get_db,
    GroupMember, Bill, BillParticipant, SplitType, DuplicatePreventionLog,
    get_or_create_member_by_line_id, 
    get_or_create_member_by_name,    
    get_bill_by_id, get_active_bills_by_group,
    get_unpaid_debts_for_member_by_line_id,
    generate_content_hash, generate_operation_hash,
    is_duplicate_operation, log_operation, cleanup_old_duplicate_logs
)
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from linebot import LineBotApi, WebhookHandler 
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage
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
    logger.info("LINE Bot API 初始化成功 (v2.8.0)。")
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

try:
    init_db()
    logger.info("分帳資料庫初始化檢查完成 (v2.8.0)。")
except Exception as e:
    logger.exception(f"分帳資料庫初始化失敗: {e}")

# --- Regex Patterns (v2.8.0) ---
ADD_BILL_PATTERN = r'^#新增支出\s+([\d\.]+)\s+(.+?)\s+((?:@\S+(?:\s+[\d\.]+)?\s*)+)$'
LIST_BILLS_PATTERN = r'^#帳單列表$'
BILL_DETAILS_PATTERN = r'^#支出詳情\s+B-(\d+)$'
SETTLE_PAYMENT_PATTERN = r'^#結帳\s+B-(\d+)\s+((?:@\S+\s*)+)$'
ARCHIVE_BILL_PATTERN = r'^#封存帳單\s+B-(\d+)$'
MY_DEBTS_PATTERN = r'^#我的欠款$'
HELP_PATTERN = r'^#幫助$'

def parse_participant_input_v271(participants_str: str, total_bill_amount_from_command: Decimal, payer_mention_name: str) \
        -> Tuple[Optional[List[Tuple[str, Decimal]]], Optional[SplitType], Optional[str]]:
    participants_to_charge: List[Tuple[str, Decimal]] = []
    error_msg = None
    split_type = None

    raw_mentions = re.findall(r'@(\S+)(?:\s+([\d\.]+))?', participants_str)

    if not raw_mentions:
        return None, None, "請至少 @提及一位參與的成員。"

    has_any_amount_specified = any(amount_str for _, amount_str in raw_mentions)
    temp_name_set = set()

    if has_any_amount_specified:
        split_type = SplitType.UNEQUAL
        current_sum_specified_by_all_mentions = Decimal(0)

        for name, amount_str in raw_mentions:
            name = name.strip()
            if name in temp_name_set: return None, None, f"參與人 @{name} 被重複提及。"
            temp_name_set.add(name)

            if not amount_str:
                return None, None, f"分別計算模式下，@{name} 未指定金額。所有提及的參與人都需要指定金額。"
            try:
                amount = Decimal(amount_str)
                if amount <= 0: return None, None, f"@{name} 的金額 ({amount_str}) 必須大於0。"
                current_sum_specified_by_all_mentions += amount
                if name != payer_mention_name:
                    participants_to_charge.append((name, amount))
            except InvalidOperation:
                return None, None, f"@{name} 的金額 ({amount_str}) 格式無效。"

        if current_sum_specified_by_all_mentions != total_bill_amount_from_command:
            return None, None, f"所有指定金額總和 ({current_sum_specified_by_all_mentions}) 與帳單總金額 ({total_bill_amount_from_command}) 不符。"
    else:
        split_type = SplitType.EQUAL
        debtors_for_equal_split_names = []
        for name, _ in raw_mentions:
            name = name.strip()
            if name in temp_name_set: return None, None, f"參與人 @{name} 被重複提及。"
            temp_name_set.add(name)
            if name != payer_mention_name:
                debtors_for_equal_split_names.append(name)

        num_debtors = len(debtors_for_equal_split_names)
        if num_debtors > 0:
            individual_share_raw = total_bill_amount_from_command / Decimal(num_debtors)
            individual_share = individual_share_raw.quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')
            current_total_calculated = Decimal(0)
            for i, name in enumerate(debtors_for_equal_split_names):
                share_to_assign = individual_share if i < num_debtors - 1 else total_bill_amount_from_command - current_total_calculated
                participants_to_charge.append((name, share_to_assign))
                current_total_calculated += share_to_assign

    return participants_to_charge, split_type, error_msg

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

    sender_mention_name = ""
    try:
        profile = line_bot_api.get_group_member_profile(group_id, sender_line_user_id)
        sender_mention_name = profile.display_name
    except LineBotApiError as e_profile:
        logger.warning(f"無法即時獲取發送者 (LINEID:{sender_line_user_id}) 在群組 {group_id} 的 Profile: {e_profile.status_code} {e_profile.error.message if e_profile.error else ''}")

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

            if add_bill_match:
                if not sender_mention_name:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="無法獲取您的群組名稱以設定為付款人。"))
                    return
                handle_add_bill_v280(reply_token, add_bill_match, group_id, sender_line_user_id, sender_mention_name, db)
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
                send_splitbill_help_v280(reply_token)
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

def handle_add_bill_v280(reply_token: str, match: re.Match, group_id: str, payer_line_user_id: str, payer_mention_name: str, db: Session):
    """新增帳單功能，加入重複操作防護"""
    total_amount_str = match.group(1)
    description = match.group(2).strip()
    participants_input_str = match.group(3).strip()

    # 生成操作hash用於防止重複操作
    operation_content = f"add_bill:{total_amount_str}:{description}:{participants_input_str}"
    operation_hash = generate_operation_hash(payer_line_user_id, "add_bill", operation_content)

    # 檢查是否為重複操作
    if is_duplicate_operation(db, operation_hash, group_id, payer_line_user_id, time_window_minutes=2):
        logger.warning(f"偵測到重複新增帳單操作 - 用戶: {payer_line_user_id}, 群組: {group_id}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 偵測到重複操作，請稍等片刻再試。"))
        return

    # 記錄此次操作
    log_operation(db, operation_hash, group_id, payer_line_user_id, "add_bill")

    if not description:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請提供支出說明。")); return
    try:
        total_bill_amount = Decimal(total_amount_str)
        if total_bill_amount <= 0: raise ValueError("總支出金額必須大於0。")
    except (InvalidOperation, ValueError) as e:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"總支出金額 '{total_amount_str}' 無效: {e}")); return

    # 確保付款人存在於該群組中
    payer_member_obj = get_or_create_member_by_line_id(db, line_user_id=payer_line_user_id, group_id=group_id, display_name=payer_mention_name)

    participants_to_charge_data, split_type, error_msg = \
        parse_participant_input_v271(participants_input_str, total_bill_amount, payer_mention_name)

    if error_msg:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"參與人解析錯誤: {error_msg}")); return

    # 生成內容hash用於防止重複建立相同內容的帳單
    content_hash = generate_content_hash(payer_member_obj.id, description, total_amount_str, participants_input_str)

    # 檢查是否已存在相同內容的帳單（在過去5分鐘內）
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    recent_bill = db.query(Bill).filter(
        Bill.content_hash == content_hash,
        Bill.group_id == group_id,
        Bill.created_at > cutoff_time
    ).first()

    if recent_bill:
        logger.warning(f"偵測到重複內容的帳單 - 群組: {group_id}, Hash: {content_hash}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"⚠️ 偵測到相似的帳單已存在 (B-{recent_bill.id})，請確認是否為重複建立。"))
        return

    bill_participants_to_create_db_objects: List[BillParticipant] = []
    for p_name, p_amount_owed in participants_to_charge_data:
        debtor_member_obj = get_or_create_member_by_name(db, name=p_name, group_id=group_id)
        bp = BillParticipant(amount_owed=p_amount_owed, is_paid=False)
        bp.debtor_member_profile = debtor_member_obj
        bill_participants_to_create_db_objects.append(bp)

    try:
        db.flush() 
        new_bill = Bill(
            group_id=group_id, description=description, total_bill_amount=total_bill_amount,
            payer_member_id=payer_member_obj.id, split_type=split_type,
            content_hash=content_hash
        )
        db.add(new_bill)
        for bp_obj in bill_participants_to_create_db_objects:
            new_bill.participants.append(bp_obj)
        db.commit()

        persisted_bill = db.query(Bill).options(
            joinedload(Bill.payer_member_profile), 
            joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
        ).filter(Bill.id == new_bill.id).first()
        if not persisted_bill: raise Exception("帳單提交後未能檢索")

        participant_details_msg = [f"@{p_bp.debtor_member_profile.name} 應付 {p_bp.amount_owed:.2f}" for p_bp in persisted_bill.participants]
        reply_msg = (
            f"✅ 新增支出 B-{persisted_bill.id}！\n名目: {persisted_bill.description}\n"
            f"付款人: @{persisted_bill.payer_member_profile.name} (您)\n"
            f"總支出: {persisted_bill.total_bill_amount:.2f}\n"
            f"類型: {'均攤' if persisted_bill.split_type == SplitType.EQUAL else '分別計算'}\n"
        )
        if participant_details_msg:
            reply_msg += f"明細 ({len(participant_details_msg)}人欠款):\n" + "\n".join(participant_details_msg)
        else:
            reply_msg += "  (此筆支出無其他人需向您付款)"
        reply_msg += f"\n\n查閱: #支出詳情 B-{persisted_bill.id}"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg))

        logger.info(f"成功新增帳單 B-{persisted_bill.id} - 群組: {group_id}, 付款人: {payer_line_user_id}")

    except IntegrityError as ie:
        db.rollback(); logger.error(f"DB完整性錯誤(新增支出 v2.8.0): {ie}", exc_info=True)
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出失敗，重複資料或成員關聯無效。"))
    except Exception as e:
        db.rollback(); logger.exception(f"新增支出時意外錯誤 (v2.8.0): {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出時發生意外錯誤。"))

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

def send_splitbill_help_v280(reply_token: str):
    """幫助訊息"""
    help_text = (
        "--- 💸 分帳機器人指令 (v2.8.0) --- \n\n"
        "🔸 新增支出 (付款人為您自己):\n"
        "  `#新增支出 <總金額> <說明> @參與人A @參與人B...` (均攤)\n"
        "    例: `#新增支出 300 午餐 @陳小美 @林真心`\n\n"
        "  `#新增支出 <總金額> <說明> @參與人A <金額A> @參與人B <金額B>...` (分別計算)\n"
        "    例: `#新增支出 1000 電影票 @小王 300 @小李 350 @自己 350`\n"
        "    (註: 所有指定金額加總需等於<總金額>)\n\n"
        "🔸 查看列表:\n  `#帳單列表`\n\n"
        "🔸 查看詳情:\n  `#支出詳情 B-ID`\n\n"
        "🔸 查看個人欠款 (限目前群組):\n  `#我的欠款`\n\n"
        "🔸 更新付款狀態 (限帳單原始付款人操作):\n  `#結帳 B-ID @已付成員1 @成員2...`\n\n"
        "🔸 封存帳單 (限帳單原始付款人操作):\n  `#封存帳單 B-ID`\n\n"
        "🔸 本說明:\n  `#幫助`\n\n"
        "✨ v2.8.0 新功能:\n"
        "- 修復群組資料隔離問題\n"
        "- 防止重複操作和帳單建立\n"
        "- 改善資料庫效能和穩定性"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 7777)) 
    host = '0.0.0.0'
    logger.info(f"分帳Bot Flask 應用 (開發伺服器 v2.8.0) 啟動於 host={host}, port={port}")
    try:
        app.run(host=host, port=port, debug=True) 
    except Exception as e:
        logger.exception(f"啟動分帳Bot Flask 應用 (開發伺服器) 時發生錯誤: {e}")