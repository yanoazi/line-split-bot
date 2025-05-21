# app_splitbill.py
from flask import Flask, request, abort, jsonify
import os
import re
from typing import List, Optional, Dict, Any, Set, Tuple
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation # For precise arithmetic

from dotenv import load_dotenv
import logging

# Use the models and db session from models_splitbill.py
from models_splitbill import (
    init_db_splitbill as init_db, # Aliasing for consistency
    get_db_splitbill as get_db,   # Aliasing for consistency
    GroupMember, Bill, BillParticipant, SplitType,get_unpaid_debts_for_member,
    get_or_create_member, get_bill_by_id, get_active_bills_by_group
)
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage # Add Flex as needed
)

app = Flask(__name__)
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# LINE Bot Configuration
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN') # Use your existing token
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')       # Use your existing secret

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.error("LINE Channel Access Token/Secret未設定。")
    exit(1)

try:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    logger.info("LINE Bot API 初始化成功。")
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

try:
    init_db() # Initializes tables from models_splitbill.py
    logger.info("分帳資料庫初始化檢查完成。")
except Exception as e:
    logger.exception(f"分帳資料庫初始化失敗: {e}")

# --- Regex Patterns ---
# #新增支出 @付款人 金額 說明 @參與人1 @參與人2 ... (equal)
# #新增支出 @付款人 金額 說明 @參與人1:金額A @參與人2:金額B ... (unequal)
ADD_BILL_PATTERN = r'^#新增支出\s+@(\S+)\s+([\d\.]+)\s+(.+?)\s+((?:@\S+(?::[\d\.]+)?\s*)+)$'
# Example: #新增支出 @Alice 100 午餐 @Bob @Charlie
# Example: #新增支出 @Alice 100 午餐 @Bob:60 @Charlie:40

LIST_BILLS_PATTERN = r'^#帳單列表$'
BILL_DETAILS_PATTERN = r'^#支出詳情\s+B-(\d+)$' # B- for Bill ID
# #結帳 B-1 @參與人1 @參與人2... (Payer marks these participants as paid for Bill B-1)
SETTLE_PAYMENT_PATTERN = r'^#結帳\s+B-(\d+)\s+((?:@\S+\s*)+)$'
ARCHIVE_BILL_PATTERN = r'^#封存帳單\s+B-(\d+)$' # To manually archive a bill
HELP_PATTERN = r'^#幫助分帳$'
MY_DEBTS_PATTERN = r'^#我的欠款$'

def parse_participant_input(participants_str: str) -> Tuple[Optional[List[Tuple[str, Optional[Decimal]]]], Optional[SplitType], Optional[str]]:
    """
    Parses the participant part of the command.
    Returns: (list_of_(name, amount_or_None), split_type, error_message)
    If amount is None, it's for equal split.
    """
    participants = []
    # Check for unequal split marker first (presence of ':')
    if ':' in participants_str:
        # Unequal split: @name1:amount1 @name2:amount2
        # Regex to find @name:amount pairs
        raw_pairs = re.findall(r'@(\S+):([\d\.]+)', participants_str)
        if not raw_pairs: # Could be malformed unequal string
             # Check if it's mixed or just bad format like "@name1: @name2:amount"
             if re.search(r'@\S+:', participants_str) and not re.search(r'@\S+:[\d\.]+', participants_str):
                return None, None, "部分參與人金額格式錯誤 (例如 @名稱:金額)。"

        temp_names = set()
        for name, amount_str in raw_pairs:
            name = name.strip()
            if not name: continue # Should not happen with \S+
            if name in temp_names:
                return None, None, f"參與人 @{name} 重複指定金額。"
            temp_names.add(name)
            try:
                amount = Decimal(amount_str)
                if amount <= 0:
                    return None, None, f"@{name} 的金額 ({amount_str}) 必須大於0。"
                participants.append((name, amount))
            except InvalidOperation:
                return None, None, f"@{name} 的金額 ({amount_str}) 格式無效。"

        # Check if there are any @mentions without amounts, which is an error for unequal
        plain_mentions = re.findall(r'@(\S+)(?!\s*:[\d\.]+)', participants_str)
        parsed_names_with_amounts = {p[0] for p in participants}
        for p_mention in plain_mentions:
            if p_mention.strip() not in parsed_names_with_amounts:
                return None, None, f"發現未指定金額的參與人 @{p_mention.strip()}，但已指定部分人金額。"

        return participants, SplitType.UNEQUAL, None

    else:
        # Equal split: @name1 @name2
        raw_names = re.findall(r'@(\S+)', participants_str)
        if not raw_names:
            return None, None, "請至少指定一位參與人。"

        temp_names = set()
        for name_str in raw_names:
            name = name_str.strip()
            if not name: continue
            if name in temp_names:
                return None, None, f"參與人 @{name} 重複指定。"
            temp_names.add(name)
            participants.append((name, None)) # Amount is None for equal split
        return participants, SplitType.EQUAL, None


@app.route("/splitbill/callback", methods=['POST']) # Different endpoint
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.info(f"分帳Bot Request body: {body}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("分帳Bot Invalid signature.")
        abort(400)
    except Exception as e:
        logger.exception(f"處理分帳Bot回調時發生未預期錯誤: {e}")
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    text = event.message.text.strip()
    reply_token = event.reply_token

    if not event.reply_token or not isinstance(event.reply_token, str) or event.reply_token == "<no-reply>":
        logger.warning(f"分帳Bot: Invalid or missing reply_token for event. Source: {event.source}")
        return

    source = event.source
    group_id: Optional[str] = None
    sender_line_user_id: str = source.user_id # Needed to map sender to GroupMember if they are payer

    if source.type == 'group':
        group_id = source.group_id
    elif source.type == 'room': # Rooms can also be used
        group_id = source.room_id
    else:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="此分帳機器人僅限群組內使用。"))
        return

    logger.info(f"分帳Bot Received from G/R ID {group_id} by User {sender_line_user_id}: '{text}'")

    try:
        with get_db() as db: # Use the splitbill DB session
            add_bill_match = re.match(ADD_BILL_PATTERN, text)
            list_bills_match = re.match(LIST_BILLS_PATTERN, text)
            bill_details_match = re.match(BILL_DETAILS_PATTERN, text)
            settle_payment_match = re.match(SETTLE_PAYMENT_PATTERN, text)
            archive_bill_match = re.match(ARCHIVE_BILL_PATTERN, text)
            help_match = re.match(HELP_PATTERN, text)

            if add_bill_match:
                handle_add_bill(reply_token, add_bill_match, group_id, sender_line_user_id, db)
            elif list_bills_match:
                handle_list_bills(reply_token, group_id, db)
            elif bill_details_match:
                bill_db_id = int(bill_details_match.group(1))
                handle_bill_details(reply_token, bill_db_id, group_id, db)
            elif settle_payment_match:
                bill_db_id = int(settle_payment_match.group(1))
                debtor_mentions_str = settle_payment_match.group(2)
                handle_settle_payment(reply_token, bill_db_id, debtor_mentions_str, group_id, sender_line_user_id, db)
            elif archive_bill_match:
                bill_db_id = int(archive_bill_match.group(1))
                handle_archive_bill(reply_token, bill_db_id, group_id, sender_line_user_id, db)
            elif help_match:
                send_splitbill_help(reply_token)
            # Add more commands as needed
            # else:
            #     logger.info(f"分帳Bot: Unmatched command '{text}' in group {group_id}")


    except SQLAlchemyError as db_err:
        logger.exception(f"分帳Bot DB錯誤: {db_err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="處理您的請求時發生資料庫錯誤。"))
    except InvalidOperation as dec_err: # Catch Decimal conversion errors early
        logger.warning(f"分帳Bot Decimal轉換錯誤: {dec_err} for text: {text}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"金額格式錯誤: {dec_err}"))
    except ValueError as val_err: # Catch other value errors from parsing
        logger.warning(f"分帳Bot 數值或格式錯誤: {val_err} for text: {text}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"輸入錯誤: {val_err}"))
    except Exception as e:
        logger.exception(f"分帳Bot 未預期錯誤: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="處理您的請求時發生內部錯誤。"))


def handle_add_bill(reply_token: str, match: re.Match, group_id: str, sender_line_id: str, db: Session):
    payer_name = match.group(1).strip()
    total_amount_str = match.group(2)
    description = match.group(3).strip()
    participants_input_str = match.group(4).strip()

    if not description:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請提供支出說明。"))
        return

    try:
        total_amount = Decimal(total_amount_str)
        if total_amount <= 0:
            raise ValueError("總金額必須大於0。")
    except InvalidOperation:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"總金額 '{total_amount_str}' 格式無效。"))
        return

    # Get or create payer. Payer must be the sender or someone identifiable.
    # For now, assume payer is identifiable by @name. Update their line_id if it's the sender.
    payer_member = get_or_create_member(db, name=payer_name, group_id=group_id)
    # If the sender is the payer, ensure their line_user_id is stored with the member
    profile = line_bot_api.get_group_member_profile(group_id, sender_line_id) # Get sender's display name
    if profile.display_name == payer_name and not payer_member.line_user_id:
        payer_member.line_user_id = sender_line_id


    parsed_participants, split_type, error_msg = parse_participant_input(participants_input_str)

    if error_msg:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"參與人格式錯誤: {error_msg}"))
        return
    if not parsed_participants: # Should be caught by error_msg but as a safeguard
        line_bot_api.reply_message(reply_token, TextSendMessage(text="無法解析參與人。"))
        return

    bill_participants_to_create: List[BillParticipant] = []
    participant_members_objects: List[GroupMember] = []

    if split_type == SplitType.EQUAL:
        if not parsed_participants: # Should have at least one
            line_bot_api.reply_message(reply_token,TextSendMessage(text="均攤時至少需要一位參與人。")); return

        num_participants = len(parsed_participants)
        try:
            # Ensure total_amount is divisible without micro-remainders for typical currencies, or handle rounding
            # For simplicity, we might allow small remainders and let the payer absorb/distribute.
            # Or, more complex: distribute remainder. For now, direct division.
            individual_share = total_amount / Decimal(num_participants)
            # Let's round to 2 decimal places for currency.
            # This can lead to small discrepancies if not handled carefully.
            # A common approach is to assign rounded shares and adjust one share for the remainder.
            # For now, simple rounding and we'll see.
            individual_share = individual_share.quantize(Decimal('0.01'))


        except ZeroDivisionError: # Should be caught by "at least one participant"
            line_bot_api.reply_message(reply_token,TextSendMessage(text="參與人數不能為零。")); return

        temp_total_check = Decimal(0)
        for p_name, _ in parsed_participants:
            member_obj = get_or_create_member(db, name=p_name, group_id=group_id)
            participant_members_objects.append(member_obj)
            # For equal split, participant object created later with Bill
            temp_total_check += individual_share

        # Distribute remainder if any due to rounding (assign to the payer if they are a participant, or first participant)
        remainder = total_amount - temp_total_check

        # Create BillParticipant objects
        for idx, p_name_tuple in enumerate(parsed_participants):
            p_name = p_name_tuple[0]
            member_obj = next(m for m in participant_members_objects if m.name == p_name) # Find already fetched/created
            current_share = individual_share
            if idx == 0 and remainder != Decimal(0): # Add remainder to the first participant's share
                current_share += remainder

            # Skip adding participant if they are the payer (payer doesn't owe themselves)
            if member_obj.id == payer_member.id:
                logger.info(f"Payer @{payer_member.name} is also a participant, skipping their debt entry.")
                num_participants -=1 # Adjust count for accurate final share if payer was included
                # Re-calculate if payer was part of the split count
                if num_participants > 0 :
                    # This logic gets complex quickly if payer is also splitting.
                    # Simplest: Payer pays, others split the cost among themselves.
                    # If payer is also part of the cost, they effectively pay less of the total bill.
                    # For THIS model: payer pays the bill, others owe the payer.
                    # So if payer is listed as participant, their share is just for calculation, they don't create a debt to themselves.
                    continue # Don't create a BillParticipant for the payer

            bp = BillParticipant(debtor_id=member_obj.id, amount_owed=current_share, is_paid=False)
            bill_participants_to_create.append(bp)


    elif split_type == SplitType.UNEQUAL:
        sum_of_specified_amounts = Decimal(0)
        for p_name, p_amount in parsed_participants:
            if p_amount is None: # Should not happen if parse_participant_input is correct
                line_bot_api.reply_message(reply_token, TextSendMessage(text="內部錯誤：非均攤解析金額失敗。")); return
            sum_of_specified_amounts += p_amount
            member_obj = get_or_create_member(db, name=p_name, group_id=group_id)
            participant_members_objects.append(member_obj)

            # Skip adding participant if they are the payer
            if member_obj.id == payer_member.id:
                logger.info(f"Payer @{payer_member.name} is also a participant in unequal split with amount {p_amount}, skipping their debt entry.")
                # The total_amount should be what others owe the payer.
                # If payer specified an amount for themselves, it means the "bill" total was higher,
                # and this is their contribution. The `total_amount` in the command is what *others combined* owe.
                # This needs clear definition. Let's assume total_amount is the full bill.
                # Payer contributes (total_amount - sum_of_others_shares).
                # The command: #addbill @Payer FullTotalAmount Description @Debtor1:Amt1 @Debtor2:Amt2 @Payer:PayerAmt
                # In this case, FullTotalAmount should equal sum of all specified amounts.
                continue # Don't create a BillParticipant for the payer to owe themselves

            bp = BillParticipant(debtor_id=member_obj.id, amount_owed=p_amount, is_paid=False)
            bill_participants_to_create.append(bp)

        # Validate sum for unequal split (excluding payer's "share" if they listed themselves)
        sum_owed_by_others = sum(bp.amount_owed for bp in bill_participants_to_create)
        if sum_owed_by_others != total_amount:
            # This check is tricky if payer also specified an amount for themselves.
            # Let's assume the `total_amount` in the command is the amount the *payer fronted* and expects back from *others*.
            # So, the sum of amounts in `@Debtor:Amount` should equal `total_amount`.
            payer_self_specified_amount = Decimal(0)
            for p_name, p_amount_opt in parsed_participants:
                 if p_name == payer_member.name and p_amount_opt is not None:
                     payer_self_specified_amount = p_amount_opt
                     break

            if payer_self_specified_amount > 0: # Payer specified their share, means total_amount was the grand total
                if sum_of_specified_amounts != total_amount:
                     line_bot_api.reply_message(reply_token, TextSendMessage(text=f"指定金額總和 ({sum_of_specified_amounts}) 與帳單總金額 ({total_amount}) 不符。"))
                     return
            else: # Payer did not specify their share, total_amount is what others owe them
                if sum_owed_by_others != total_amount:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text=f"參與人指定金額總和 ({sum_owed_by_others}) 與支出金額 ({total_amount}) 不符。"))
                    return

    if not bill_participants_to_create:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="沒有需要分帳的參與人 (可能是只有付款人)。"))
        return

    try:
        new_bill = Bill(
            group_id=group_id,
            description=description,
            total_amount=total_amount, # This is the amount the Payer paid / expects back from others
            payer_id=payer_member.id,
            split_type=split_type,
        )
        # Add participants to the bill
        for bp_obj in bill_participants_to_create:
            new_bill.participants.append(bp_obj)

        db.add(new_bill)
        db.add_all(participant_members_objects) # Add any newly created member objects
        db.add(payer_member) # Ensure payer member (possibly updated with line_id) is also added/updated
        db.commit()

        # Send confirmation
        participant_details = []
        for bp in new_bill.participants: # Iterate over persisted participants
            # Need to fetch debtor_member name if not already loaded
            debtor = db.query(GroupMember).filter(GroupMember.id == bp.debtor_id).first()
            if debtor:
                participant_details.append(f"@{debtor.name} 應付 {bp.amount_owed:.2f}")

        reply_msg = (
            f"✅ 新增支出 B-{new_bill.id}成功！\n"
            f"說明: {new_bill.description}\n"
            f"付款人: @{payer_member.name}\n"
            f"總金額: {new_bill.total_amount:.2f}\n"
            f"類型: {'均攤' if new_bill.split_type == SplitType.EQUAL else '分別計算'}\n"
            f"參與人 ({len(participant_details)}人):\n" +
            "\n".join(participant_details) +
            f"\n\n使用 #支出詳情 B-{new_bill.id} 查看。"
        )
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg))

    except IntegrityError as ie:
        db.rollback()
        logger.error(f"資料庫完整性錯誤 (新增支出): {ie}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出失敗，可能存在重複資料或無效的成員ID。"))
    except Exception as e:
        db.rollback()
        logger.exception(f"新增支出時發生錯誤: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增支出時發生未預期錯誤。"))


def handle_list_bills(reply_token: str, group_id: str, db: Session):
    bills = get_active_bills_by_group(db, group_id)
    if not bills:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="目前沒有待處理的帳單。"))
        return

    # For simplicity, sending as text. Flex message would be nicer.
    reply_items = [f"--- 📜 本群組帳單列表 (未封存) ---"]
    for bill in bills:
        item = (
            f"\nID: B-{bill.id} | {bill.description}\n"
            f"付款人: @{bill.payer.name} | 總金額: {bill.total_amount:.2f}\n"
            f"類型: {'均攤' if bill.split_type == SplitType.EQUAL else '分別計算'}\n"
            f"參與情況:"
        )
        all_paid_for_bill = True
        if not bill.participants:
            item += " (尚無參與人分帳)"
            all_paid_for_bill = False # Or true if no one to pay
        else:
            for p in bill.participants:
                paid_status = "✅已付" if p.is_paid else "🅾️未付"
                item += f"\n  @{p.debtor_member.name}: {p.amount_owed:.2f} ({paid_status})"
                if not p.is_paid:
                    all_paid_for_bill = False

        if all_paid_for_bill and bill.participants: # only add if there were participants
             item += "\n✨ 此帳單所有參與人均已結清！"
        item += f"\n(詳情: #支出詳情 B-{bill.id})"
        reply_items.append(item)

    full_reply = "\n".join(reply_items)
    if len(full_reply) > 4950: # LINE message limit approx 5000
        full_reply = full_reply[:4950] + "\n...(訊息過長，部分省略)"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply))

def handle_bill_details(reply_token: str, bill_db_id: int, group_id: str, db: Session):
    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到ID為 B-{bill_db_id} 的帳單，或該帳單不屬於本群組。"))
        return

    # Construct detailed message (similar to list, but for one bill)
    paid_count = sum(1 for p in bill.participants if p.is_paid)
    total_participants = len(bill.participants)

    reply_msg = (
        f"--- 💳 支出詳情: B-{bill.id} ---\n"
        f"說明: {bill.description}\n"
        f"付款人: @{bill.payer.name}\n"
        f"總金額: {bill.total_amount:.2f}\n"
        f"類型: {'均攤' if bill.split_type == SplitType.EQUAL else '分別計算'}\n"
        f"建立於: {bill.created_at.strftime('%Y/%m/%d %H:%M') if bill.created_at else 'N/A'}\n"
        f"狀態: {'已封存' if bill.is_archived else '處理中'}\n"
        f"付款進度: {paid_count}/{total_participants} 位參與人已結帳\n"
        f"參與人明細:"
    )
    if not bill.participants:
        reply_msg += "\n  (尚無參與人分帳紀錄)"
    else:
        for p in bill.participants:
            paid_status_icon = "✅" if p.is_paid else "🅾️"
            paid_date_str = f"(於 {p.paid_at.strftime('%y/%m/%d')})" if p.is_paid and p.paid_at else ""
            reply_msg += f"\n  {paid_status_icon} @{p.debtor_member.name} 應付 {p.amount_owed:.2f} {paid_date_str}"

    if not bill.is_archived:
        reply_msg += f"\n\n➡️ 付款人可使用 `#結帳 B-{bill.id} @提及已付款成員` 來更新狀態。"
        if total_participants > 0 and paid_count == total_participants:
             reply_msg += f"\n➡️ 所有款項已結清！可使用 `#封存帳單 B-{bill.id}` 將其移出列表。"
        elif total_participants == 0:
             reply_msg += f"\n➡️ 此帳單尚無分帳人，可考慮 `#封存帳單 B-{bill.id}` 或編輯。"


    if len(reply_msg) > 4950:
        reply_msg = reply_msg[:4950] + "\n...(訊息過長)"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_msg))


def handle_settle_payment(reply_token: str, bill_db_id: int, debtor_mentions_str: str, group_id: str, sender_line_id: str, db: Session):
    bill = get_bill_by_id(db, bill_db_id, group_id) # This already loads participants and payer
    if not bill:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到ID為 B-{bill_db_id} 的帳單，或該帳單不屬於本群組。"))
        return

    # Authorization: Only the original payer of THIS bill can mark others as paid.
    # We need to ensure the sender_line_id corresponds to the bill.payer.line_user_id
    # First, ensure payer's line_user_id is known.
    if not bill.payer.line_user_id:
        # Attempt to update payer's line_user_id if they are the sender
        if bill.payer.name == line_bot_api.get_group_member_profile(group_id, sender_line_id).display_name:
            bill.payer.line_user_id = sender_line_id
            # db.commit() # Commit this small update, or do it at the end. For now, assume it's part of larger transaction.
            logger.info(f"Updated payer @{bill.payer.name}'s line_user_id to {sender_line_id} during settle command.")
        else: # Payer's line_id unknown and sender is not obviously the payer by name
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"無法驗證付款人身份來結帳。付款人 @{bill.payer.name} 需先與機器人互動或被正確記錄。"))
            return

    if bill.payer.line_user_id != sender_line_id:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"只有此帳單的付款人 @{bill.payer.name} 才能執行結帳操作。"))
        return

    if bill.is_archived:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 已封存，無法修改付款狀態。"))
        return

    debtor_names_to_settle = {name.strip() for name in re.findall(r'@(\S+)', debtor_mentions_str) if name.strip()}
    if not debtor_names_to_settle:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請 @提及 至少一位要標記為已付款的參與人。"))
        return

    settled_count = 0
    already_paid_names = []
    not_found_names = list(debtor_names_to_settle) # Start with all, remove if found

    for bp in bill.participants:
        # debtor_member should be loaded by get_bill_by_id
        if bp.debtor_member.name in debtor_names_to_settle:
            not_found_names.remove(bp.debtor_member.name) # Found this mentioned debtor
            if not bp.is_paid:
                bp.is_paid = True
                bp.paid_at = datetime.now(timezone.utc)
                settled_count += 1
            else:
                already_paid_names.append(f"@{bp.debtor_member.name}")

    if settled_count > 0:
        db.commit()

    reply_parts = []
    if settled_count > 0:
        reply_parts.append(f"已成功為 B-{bill_db_id} 標記 {settled_count} 位參與人付款。")
    if already_paid_names:
        reply_parts.append(f"提示: {', '.join(already_paid_names)} 先前已標記付款。")
    if not_found_names:
        reply_parts.append(f"注意: 找不到以下參與人於此帳單中或提及名稱錯誤: {', '.join(['@'+n for n in not_found_names])}。")

    if not reply_parts: # e.g. only mentioned people not in bill
        line_bot_api.reply_message(reply_token, TextSendMessage(text="沒有有效的參與人被更新付款狀態。"))
    else:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="\n".join(reply_parts)))

def handle_archive_bill(reply_token: str, bill_db_id: int, group_id: str, sender_line_id: str, db: Session):
    bill = get_bill_by_id(db, bill_db_id, group_id)
    if not bill:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到帳單 B-{bill_db_id}。"))
        return

    # Optional: Authorization - only payer or specific roles can archive
    # For now, let's allow anyone in the group to archive a fully paid bill, or the payer anytime
    is_sender_payer = bill.payer.line_user_id == sender_line_id
    if not is_sender_payer: # If sender is not confirmed payer, check if all paid
        all_paid = all(p.is_paid for p in bill.participants) if bill.participants else True # True if no participants
        if not all_paid:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 尚有未結清款項，只有付款人 @{bill.payer.name} 可提前封存。"))
            return

    if bill.is_archived:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"帳單 B-{bill_db_id} 已經是封存狀態。"))
        return

    bill.is_archived = True
    try:
        db.commit()
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ 帳單 B-{bill_db_id} ({bill.description[:20]}...) 已成功封存，將不再顯示於 #帳單列表。"))
    except Exception as e:
        db.rollback()
        logger.error(f"封存帳單 B-{bill_db_id} 失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"封存帳單 B-{bill_db_id} 時發生錯誤。"))

def handle_my_debts(reply_token: str, sender_mention_name: str, group_id: str, db: Session):
    logger.info(f"使用者 @{sender_mention_name} (group: {group_id}) 查詢個人欠款。")

    # Find the GroupMember corresponding to the sender's mention name in this group
    member = db.query(GroupMember).filter_by(name=sender_mention_name, group_id=group_id).first()

    if not member:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"@{sender_mention_name}，您似乎還沒有參與過本群組的任何分帳活動，或您的名稱記錄有誤。"))
        return

    unpaid_participations = get_unpaid_debts_for_member(db, member.id, group_id)

    if not unpaid_participations:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"@{sender_mention_name}，您目前在本群組沒有任何未付清的款項！🎉"))
        return

    reply_items = [f"--- 💸 @{sender_mention_name} 的未付款項 ---"]
    total_owed_all_bills = Decimal(0)

    for bp in unpaid_participations:
        # bill and payer should be loaded by the query
        bill = bp.bill
        payer = bill.payer 

        item = (
            f"\n帳單 B-{bill.id}: {bill.description}\n"
            f"  應付 @{payer.name}: {bp.amount_owed:.2f}\n"
            f"  (詳情: #支出詳情 B-{bill.id})"
        )
        reply_items.append(item)
        total_owed_all_bills += bp.amount_owed

    reply_items.append(f"\n--------------------\n कुल欠款總額: {total_owed_all_bills:.2f}")

    full_reply = "\n".join(reply_items)
    if len(full_reply) > 4950: # LINE message limit
        full_reply = full_reply[:4950] + "\n...(訊息過長，部分省略)"

    line_bot_api.reply_message(reply_token, TextSendMessage(text=full_reply))

# app_splitbill.py

# ... (all imports and other function definitions like handle_add_bill, etc., should be above this) ...

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    text = event.message.text.strip()
    reply_token = event.reply_token # reply_token is now checked and asserted as str

    # Assert reply_token is valid (as per previous fix v2.5.1 style)
    if not event.reply_token or not isinstance(event.reply_token, str) or event.reply_token == "<no-reply>":
        logger.warning(f"分帳Bot: Invalid or missing reply_token for event. Source: {event.source}")
        return
    # reply_token is now guaranteed to be a str if we proceed

    source = event.source
    group_id: Optional[str] = None
    sender_line_user_id: str = source.user_id

    if source.type == 'group':
        group_id = source.group_id
    elif source.type == 'room': 
        group_id = source.room_id
    else:
        # This reply_token is valid here
        line_bot_api.reply_message(reply_token, TextSendMessage(text="此分帳機器人僅限群組內使用。"))
        return

    logger.info(f"分帳Bot Received from G/R ID {group_id} by User {sender_line_user_id}: '{text}'")

    # Main try block for all command processing
    try:
        with get_db() as db: # Use the splitbill DB session
            add_bill_match = re.match(ADD_BILL_PATTERN, text)
            list_bills_match = re.match(LIST_BILLS_PATTERN, text)
            bill_details_match = re.match(BILL_DETAILS_PATTERN, text)
            settle_payment_match = re.match(SETTLE_PAYMENT_PATTERN, text)
            archive_bill_match = re.match(ARCHIVE_BILL_PATTERN, text)
            my_debts_match = re.match(MY_DEBTS_PATTERN, text) # Your specific snippet context
            help_match = re.match(HELP_PATTERN, text)

            if add_bill_match:
                handle_add_bill(reply_token, add_bill_match, group_id, sender_line_user_id, db)
            elif list_bills_match:
                handle_list_bills(reply_token, group_id, db)
            elif bill_details_match:
                bill_db_id = int(bill_details_match.group(1))
                handle_bill_details(reply_token, bill_db_id, group_id, db)
            elif settle_payment_match:
                bill_db_id = int(settle_payment_match.group(1))
                debtor_mentions_str = settle_payment_match.group(2)
                handle_settle_payment(reply_token, bill_db_id, debtor_mentions_str, group_id, sender_line_user_id, db)
            elif archive_bill_match:
                bill_db_id = int(archive_bill_match.group(1))
                handle_archive_bill(reply_token, bill_db_id, group_id, sender_line_user_id, db)

            # This is your provided snippet, correctly placed:
            elif my_debts_match:
                # This command requires knowing the sender's @mention name to find their GroupMember record
                try:
                    # Ensure group_id is valid before using it (it should be, as we returned earlier if not group/room)
                    if not group_id: # Defensive check, should have been caught by initial source type check
                        logger.error(f"Error in #我的欠款: group_id is None for sender {sender_line_user_id}. This should not happen.")
                        line_bot_api.reply_message(reply_token, TextSendMessage(text="查詢欠款時發生內部錯誤 (無法識別群組)。"))
                        return # Exit if group_id is somehow still None

                    sender_profile = line_bot_api.get_group_member_profile(group_id, sender_line_user_id)
                    sender_mention_name = sender_profile.display_name
                    handle_my_debts(reply_token, sender_mention_name, group_id, db)
                except Exception as e_profile: # Catching errors from LINE API or handle_my_debts
                    logger.error(f"無法獲取發送者 ({sender_line_user_id}) 在群組 {group_id} 的個人資料或處理欠款: {e_profile}", exc_info=True)
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，無法查詢您的欠款記錄，可能是因為無法取得您的群組名稱或內部處理錯誤。"))

            elif help_match:
                send_splitbill_help(reply_token)
            else:
                logger.info(f"分帳Bot: Unmatched command '{text}' in group {group_id}")
                # Optional: Reply for unmatched commands in groups if desired
                # line_bot_api.reply_message(reply_token, TextSendMessage(text=f"無法識別指令：{text[:30]}...\n請輸入 #幫助分帳 查看。"))

    # These are the critical `except` blocks for the main `try`
    except SQLAlchemyError as db_err:
        logger.exception(f"分帳Bot DB錯誤: {db_err}") # .exception logs traceback
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="處理您的請求時發生資料庫錯誤。"))
        except Exception as reply_err:
            logger.error(f"分帳Bot 回覆DB錯誤訊息失敗: {reply_err}")
    except InvalidOperation as dec_err: 
        logger.warning(f"分帳Bot Decimal轉換錯誤: {dec_err} for text: {text}")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"金額格式錯誤: {dec_err}"))
        except Exception as reply_err:
            logger.error(f"分帳Bot 回覆Decimal錯誤訊息失敗: {reply_err}")
    except ValueError as val_err: 
        logger.warning(f"分帳Bot 數值或格式錯誤: {val_err} for text: {text}")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"輸入錯誤: {val_err}"))
        except Exception as reply_err:
            logger.error(f"分帳Bot 回覆ValueError訊息失敗: {reply_err}")
    except Exception as e: # Catch-all for other unexpected errors
        logger.exception(f"分帳Bot 未預期錯誤: {e}") # .exception logs traceback
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="處理您的請求時發生內部錯誤。"))
        except Exception as reply_err:
            logger.error(f"分帳Bot 回覆內部錯誤訊息失敗: {reply_err}")

# Make sure this function definition starts after the handle_text_message function has fully ended
# def send_splitbill_help(reply_token: str):
#     # ... implementation ...

# ... (rest of your app_splitbill.py code)

def send_splitbill_help(reply_token: str):
    help_text = (
        "--- 💸 分帳機器人指令 --- \n\n"
        "🔸 新增支出 (均攤):\n"
        "`#新增支出 @付款人 金額 說明 @參與人1 @參與人2...`\n"
        "   範例: `#新增支出 @王大陸 300 午餐 @陳小美 @林真心`\n\n"
        "🔸 新增支出 (分別計算):\n"
        "`#新增支出 @付款人 總金額 說明 @參與人A:金額A @參與人B:金額B...`\n"
        "   範例: `#新增支出 @艾莉絲 1000 電影票 @鮑伯:350 @查理:350 @艾莉絲:300`\n"
        "   (注意: 參與人指定金額總和需等於總金額)\n\n"
        "🔸 查看列表:\n"
        "`#帳單列表` - 顯示本群組所有未封存帳單\n\n"
        "🔸 查看詳情:\n"
        "`#支出詳情 B-ID` (ID從列表中取得)\n"
        "   範例: `#支出詳情 B-5`\n\n"
        "🔸 更新付款狀態 (由帳單付款人操作):\n"
        "`#結帳 B-ID @已付款的參與人1 @參與人2...`\n"
        "   範例: `#結帳 B-5 @陳小美 @林真心`\n\n"
        "🔸 查看個人欠款:\n"
        "`#我的欠款` - 顯示您在本群組所有未付清的款項\n\n"
        "🔸 封存帳單 (移出列表):\n"
        "`#封存帳單 B-ID`\n"
        "   (付款人可隨時封存；其他人需帳單全結清後才可封存)\n\n"
        "🔸 本說明:\n"
        "`#幫助分帳`"
    )
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))


if __name__ == "__main__":
    # Make sure to set a different port if running alongside another Flask app
    # or ensure your deployment handles multiple apps correctly.
    port = int(os.environ.get('PORT', 7777)) # Use a different port
    host = '0.0.0.0'
    logger.info(f"分帳Bot Flask 應用啟動於 host={host}, port={port}")
    try:
        app.run(host=host, port=port, debug=False)
    except Exception as e:
        logger.exception(f"啟動分帳Bot Flask 應用時發生未預期錯誤: {e}")
