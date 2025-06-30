# app_splitbill.py (v2.7.1 - Space Separated Amounts, LINE User ID for Payer)
from flask import Flask, request, abort, jsonify
import os
import re
from typing import List, Optional, Dict, Any, Set, Tuple
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
import logging

# Assuming models_splitbill.py is v2.7.0 as discussed
from models_splitbill import (
    init_db_splitbill as init_db,
    get_db_splitbill as get_db,
    GroupMember, Bill, BillParticipant, SplitType,
    get_or_create_member_by_line_id, 
    get_or_create_member_by_name,    
    get_bill_by_id, get_active_bills_by_group,
    get_unpaid_debts_for_member_by_line_id 
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
    logger.info("LINE Bot API 初始化成功 (v2.7.1)。")
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

try:
    init_db()
    logger.info("分帳資料庫初始化檢查完成 (v2.7.1)。")
except Exception as e:
    logger.exception(f"分帳資料庫初始化失敗: {e}")

# --- Regex Patterns (v2.7.1) ---
ADD_BILL_PATTERN = r'^#新增支出\s+([\d\.]+)\s+(.+?)\s+((?:@\S+(?:\s+[\d\.]+)?\s*)+)$'
LIST_BILLS_PATTERN = r'^#帳單列表$'
BILL_DETAILS_PATTERN = r'^#支出詳情\s+B-(\d+)$'
SETTLE_PAYMENT_PATTERN = r'^#結帳\s+B-(\d+)\s+((?:@\S+\s*)+)$'
ARCHIVE_BILL_PATTERN = r'^#封存帳單\s+B-(\d+)$'
MY_DEBTS_PATTERN = r'^#我的欠款$'
HELP_PATTERN = r'^#幫助$' # Changed command

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
                handle_add_bill_v271(reply_token, add_bill_match, group_id, sender_line_user_id, sender_mention_name, db)
            elif list_bills_match:
                handle_list_bills(reply_token, group_id, db)
            elif bill_details_match:
                bill_db_id = int(bill_details_match.group(1))
                handle_bill_details(reply_token, bill_db_id, group_id, db)
            elif settle_payment_match:
                # sender_line_user_id is used for auth inside handle_settle_payment_v271
                bill_db_id = int(settle_payment_match.group(1))
                debtor_mentions_str = settle_payment_match.group(2)
                handle_settle_payment_v271(reply_token, bill_db_id, debtor_mentions_str, group_id, sender_line_user_id, db)
            elif archive_bill_match:
                # sender_line_user_id is used for auth inside handle_archive_bill_v271
                bill_db_id = int(archive_bill_match.group(1))
                handle_archive_bill_v271(reply_token, bill_db_id, group_id, sender_line_user_id, db)
            elif my_debts_match:
                handle_my_debts_v271(reply_token, sender_line_user_id, group_id, db)
            elif help_match:
                send_splitbill_help_v271(reply_token) # Updated help function name
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

def handle_add_bill_v271(reply_token: str, match: re.Match, group_id: str, payer_line_user_id: str, payer_mention_name: str, db: Session):
    total_amount_str = match.group(1)
    description = match.group(2).strip()
    participants_input_str = match.group(3).strip()

    if not description:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請提供支出說明。")); return
    try:
        total_bill_amount = Decimal(total_amount_str)
        if total_bill_amount <= 0: raise ValueError("總支出金額必須大於0。")
    except (InvalidOperation, ValueError) as e:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"總支出金額 '{total_amount_str}' 無效: {e}")); return

    payer_member_obj = get_or_create_member_by_line_id(db, line_user_id=payer_line_user_id, group_id=group_id, display_name=payer_mention_name)

    participants_to_charge_data, split_type, error_msg = \
        parse_participant_input_v271(participants_input_str, total_bill_amount, payer_mention_name)

    if error_msg:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"參與人解析錯誤: {error_msg}")); return

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

    except IntegrityError as ie:
        db.rollback(); logger.error(f"DB完整性錯誤(新增支出 v2.7.1): {ie}", exc_info=True)
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出失敗，重複資料或成員關聯無效。"))
    except Exception as e:
        db.rollback(); logger.exception(f"新增支出時意外錯誤 (v2.7.1): {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出時發生意外錯誤。"))

def handle_list_bills(reply_token: str, group_id: str, db: Session):
    bills = get_active_bills_by_group(db, group_id)
    if not bills: line_bot_api.reply_message(reply_token, TextSendMessage(text="目前無待處理帳單。")); return
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
    full_reply = "\n".join(reply_items); line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply[:4950] + ("..." if len(full_reply)>4950 else "")))

def handle_bill_details(reply_token: str, bill_db_id: int, group_id: str, db: Session):
    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。")); return
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
        if bill.participants and paid_count == total_participants: # Ensure there were participants
            reply_msg += f"\n➡️ 已結清！付款人可 `#封存帳單 B-{bill.id}`。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg[:4950] + ("..." if len(reply_msg)>4950 else "")))

def handle_settle_payment_v271(reply_token: str, bill_db_id: int, debtor_mentions_str: str, group_id: str, sender_line_user_id: str, db: Session):
    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。")); return

    if not bill.payer_member_profile.line_user_id or bill.payer_member_profile.line_user_id != sender_line_user_id:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"只有此帳單的付款人 @{bill.payer_member_profile.name} 才能執行結帳。")); return
    if bill.is_archived: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 已封存。")); return

    debtor_names_to_settle = {name.strip() for name in re.findall(r'@(\S+)', debtor_mentions_str) if name.strip()}
    if not debtor_names_to_settle: line_bot_api.reply_message(reply_token, TextSendMessage(text="請 @提及 至少一位要標記付款的參與人。")); return

    settled_count, already_paid_names, not_found_names = 0, [], list(debtor_names_to_settle)
    for bp in bill.participants:
        if bp.debtor_member_profile.name in debtor_names_to_settle:
            if bp.debtor_member_profile.name in not_found_names: not_found_names.remove(bp.debtor_member_profile.name)
            if not bp.is_paid: bp.is_paid = True; bp.paid_at = datetime.now(timezone.utc); settled_count += 1
            else: already_paid_names.append(f"@{bp.debtor_member_profile.name}")
    if settled_count > 0: db.commit()

    reply_parts = []
    if settled_count > 0: reply_parts.append(f"已為 B-{bill_db_id} 標記 {settled_count} 人付款。")
    if already_paid_names: reply_parts.append(f"提示: {', '.join(already_paid_names)} 先前已付。")
    if not_found_names: reply_parts.append(f"注意: 於此帳單找不到參與人: {', '.join(['@'+n for n in not_found_names])}。")
    line_bot_api.reply_message(reply_token, TextSendMessage(text="\n".join(reply_parts) if reply_parts else "無效操作或未提及有效參與人。"))

def handle_archive_bill_v271(reply_token: str, bill_db_id: int, group_id: str, sender_line_user_id: str, db: Session):
    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。")); return

    if not bill.payer_member_profile.line_user_id or bill.payer_member_profile.line_user_id != sender_line_user_id:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"只有此帳單的付款人 @{bill.payer_member_profile.name} 才能執行封存。")); return
    if bill.is_archived: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 已封存。")); return

    bill.is_archived = True
    try:
        db.commit()
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ 帳單 B-{bill_db_id} ({bill.description[:20]}...) 已封存。"))
    except Exception as e: db.rollback(); logger.error(f"封存帳單 B-{bill_db_id} 失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="封存失敗。"))

def handle_my_debts_v271(reply_token: str, sender_line_user_id: str, group_id: str, db: Session):
    unpaid_participations = get_unpaid_debts_for_member_by_line_id(db, sender_line_user_id, group_id)

    sender_display_name_for_msg = "您" # Default
    try: # Try to get current display name for better message personalization
        profile = line_bot_api.get_group_member_profile(group_id, sender_line_user_id)
        sender_display_name_for_msg = f"@{profile.display_name}"
    except Exception: logger.warning(f"無法獲取 {sender_line_user_id} 在群組 {group_id} 的名稱用於 #我的欠款 回覆。")

    if not unpaid_participations:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"{sender_display_name_for_msg}目前在本群組無未付款項！🎉")); return

    reply_items = [f"--- 💸 {sender_display_name_for_msg} 的未付款項 ---"]; total_owed_all_bills = Decimal(0)
    for bp in unpaid_participations:
        reply_items.append(f"\n帳單 B-{bp.bill.id}: {bp.bill.description}\n  應付 @{bp.bill.payer_member_profile.name}: {bp.amount_owed:.2f}\n  (詳情: #支出詳情 B-{bp.bill.id})")
        total_owed_all_bills += bp.amount_owed
    reply_items.append(f"\n--------------------\n欠款總額: {total_owed_all_bills:.2f}") # " कुल" removed
    full_reply = "\n".join(reply_items); line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply[:4950] + ("..." if len(full_reply)>4950 else "")))

def send_splitbill_help_v271(reply_token: str):
    help_text = (
        "--- 💸 分帳機器人指令 (v2.7.1) --- \n\n"
        "🔸 新增支出 (付款人為您自己):\n"
        "  `#新增支出 <總金額> <說明> @參與人A @參與人B...` (均攤 注意：需先扣除付款人的支付金額)\n"
        "    例: `#新增支出 300 午餐 @陳小美 @林真心`\n\n"
        "  `#新增支出 <總金額> <說明> @參與人A <金額A> @參與人B <金額B>...` (分別計算 注意：不含付款人本人)\n"
        "    例: `#新增支出 1000 電影票 @小王 300 @小李 350 @自己 350`\n"
        "    (註: 所有指定金額加總需等於<總金額>)\n\n"
        "🔸 查看列表:\n  `#帳單列表`\n\n"
        "🔸 查看詳情:\n  `#支出詳情 B-ID`\n\n"
        "🔸 查看個人欠款 (限目前群組):\n  `#我的欠款`\n\n"
        "🔸 更新付款狀態 (限帳單原始付款人操作):\n  `#結帳 B-ID @已付成員1 @成員2...`\n\n"
        "🔸 封存帳單 (限帳單原始付款人操作):\n  `#封存帳單 B-ID`\n\n"
        "🔸 本說明:\n  `#幫助`"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 7777)) 
    host = '0.0.0.0'
    logger.info(f"分帳Bot Flask 應用 (開發伺服器 v2.7.1) 啟動於 host={host}, port={port}")
    try:
        app.run(host=host, port=port, debug=True) 
    except Exception as e:
        logger.exception(f"啟動分帳Bot Flask 應用 (開發伺服器) 時發生錯誤: {e}")