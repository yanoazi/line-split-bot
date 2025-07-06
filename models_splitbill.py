# models_splitbill.py (v1.0.4 - 移除我的欠款，優化群組欠款)
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
    UniqueConstraint, Boolean, Numeric, Enum as SQLAEnum, Index
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session, joinedload
from typing import Optional, List
from sqlalchemy.sql import func
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import enum
import hashlib
import time

class SplitType(enum.Enum):
    EQUAL = "equal"      # 均攤
    UNEQUAL = "unequal"  # 分別計算

from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
DATABASE_URL = os.environ.get('DATABASE_URL_SPLITBILL', os.environ.get('DATABASE_URL'))
if not DATABASE_URL:
    raise ValueError("環境變數 DATABASE_URL_SPLITBILL 或 DATABASE_URL 未設定！")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

@contextmanager
def get_db_splitbill():
    db: Session = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

class GroupMember(Base):
    __tablename__ = "sb_group_members"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)  # @mention name in the group
    group_id = Column(String, nullable=False, index=True)  # LINE Group ID
    # LINE User ID - 不再設為全域唯一，允許同一用戶在多個群組中
    line_user_id = Column(String, index=True, nullable=True)  # 移除 unique=True
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # 關聯關係
    paid_bills = relationship("Bill", back_populates="payer_member_profile", foreign_keys="[Bill.payer_member_id]")
    bill_participations = relationship("BillParticipant", back_populates="debtor_member_profile", foreign_keys="[BillParticipant.debtor_member_id]")

    __table_args__ = (
        # 確保在同一群組中，名稱是唯一的
        UniqueConstraint('name', 'group_id', name='_sb_member_name_group_uc'),
        # 確保在同一群組中，line_user_id是唯一的（如果不為NULL）
        UniqueConstraint('line_user_id', 'group_id', name='_sb_line_user_group_uc'),
        # 提升查詢效能的複合索引
        Index('ix_sb_group_members_group_line_user', 'group_id', 'line_user_id'),
        Index('ix_sb_group_members_group_name', 'group_id', 'name'),
    )

    def __repr__(self):
        return f"<GroupMember(id={self.id}, name='{self.name}', group_id='{self.group_id}', line_id='{self.line_user_id}')>"

class Bill(Base):
    __tablename__ = "sb_bills"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=False)
    total_bill_amount = Column(Numeric(10, 2), nullable=False) 
    
    # 外鍵到 GroupMember 表的付款人
    payer_member_id = Column(Integer, ForeignKey('sb_group_members.id', ondelete='CASCADE'), nullable=False)
    payer_member_profile = relationship("GroupMember", back_populates="paid_bills", foreign_keys=[payer_member_id])

    split_type = Column(SQLAEnum(SplitType, name="sb_split_type_enum"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    is_archived = Column(Boolean, default=False, nullable=False)
    
    # 新增：用於防止重複建立的hash值 - 設為必填
    content_hash = Column(String(64), nullable=False, index=True)

    # 關聯關係
    participants = relationship("BillParticipant", back_populates="bill", cascade="all, delete-orphan")

    __table_args__ = (
        # 提升查詢效能的索引
        Index('ix_sb_bills_group_archived', 'group_id', 'is_archived'),
        Index('ix_sb_bills_group_created', 'group_id', 'created_at'),
        # 新增：資料庫層面的重複防護 - 同一群組內相同content_hash只能有一筆
        UniqueConstraint('group_id', 'content_hash', name='_sb_bill_content_unique'),
    )

    def __repr__(self):
        return f"<Bill(id={self.id}, group='{self.group_id}', desc='{self.description[:20]}', amount={self.total_bill_amount}, payer_id={self.payer_member_id})>"

class BillParticipant(Base):
    __tablename__ = "sb_bill_participants"
    id = Column(Integer, primary_key=True, index=True)

    bill_id = Column(Integer, ForeignKey('sb_bills.id', ondelete='CASCADE'), nullable=False)
    bill = relationship("Bill", back_populates="participants")

    debtor_member_id = Column(Integer, ForeignKey('sb_group_members.id', ondelete='CASCADE'), nullable=False)
    debtor_member_profile = relationship("GroupMember", back_populates="bill_participations", foreign_keys=[debtor_member_id])

    amount_owed = Column(Numeric(10, 2), nullable=False)
    is_paid = Column(Boolean, default=False, nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # 確保同一帳單中，每個債務人只能有一筆記錄
        UniqueConstraint('bill_id', 'debtor_member_id', name='_sb_bill_debtor_uc'),
        # 提升查詢效能的索引
        Index('ix_sb_bill_participants_bill_paid', 'bill_id', 'is_paid'),
        Index('ix_sb_bill_participants_debtor_paid', 'debtor_member_id', 'is_paid'),
    )

    def __repr__(self):
        return f"<BillParticipant(bill_id={self.bill_id}, debtor_id={self.debtor_member_id}, owed={self.amount_owed}, paid={self.is_paid})>"

# 新增：重複操作記錄表
class DuplicatePreventionLog(Base):
    __tablename__ = "sb_duplicate_prevention_log"
    id = Column(Integer, primary_key=True, index=True)
    operation_hash = Column(String(64), nullable=False, index=True)
    group_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    operation_type = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # 防止重複操作的複合索引
        Index('ix_sb_dup_prev_hash_group_user', 'operation_hash', 'group_id', 'user_id'),
    )

def generate_content_hash(payer_id: int, description: str, amount: str, participants_str: str) -> str:
    """生成內容hash用於防止重複建立"""
    content = f"{payer_id}:{description}:{amount}:{participants_str}"
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

def generate_operation_hash(user_id: str, operation: str, content: str) -> str:
    """生成操作hash用於防止重複操作"""
    operation_content = f"{user_id}:{operation}:{content}"
    return hashlib.sha256(operation_content.encode('utf-8')).hexdigest()

def is_duplicate_operation(db: Session, operation_hash: str, group_id: str, user_id: str, 
                          time_window_minutes: int = 2) -> bool:
    """檢查是否為重複操作（在指定時間窗口內）"""
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=time_window_minutes)
    
    existing_log = db.query(DuplicatePreventionLog).filter(
        DuplicatePreventionLog.operation_hash == operation_hash,
        DuplicatePreventionLog.group_id == group_id,
        DuplicatePreventionLog.user_id == user_id,
        DuplicatePreventionLog.created_at > cutoff_time
    ).first()
    
    return existing_log is not None

def log_operation(db: Session, operation_hash: str, group_id: str, user_id: str, operation_type: str):
    """記錄操作以防止重複"""
    log_entry = DuplicatePreventionLog(
        operation_hash=operation_hash,
        group_id=group_id,
        user_id=user_id,
        operation_type=operation_type
    )
    db.add(log_entry)
    db.flush()

def init_db_splitbill():
    logger.info("初始化分帳資料庫 (v1.0 - Fixed Group Isolation & Duplicate Prevention)，嘗試建立表格...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("分帳表格建立完成 (如果原本不存在的話)。")
    except Exception as e:
        logger.exception(f"初始化分帳資料庫時發生錯誤: {e}")

def get_or_create_member_by_line_id(db: Session, line_user_id: str, group_id: str, display_name: str) -> GroupMember:
    """
    根據LINE User ID在特定群組中獲取或創建成員
    v1.0 強化：修復併發競爭條件問題
    """
    # 使用重試機制處理競爭條件
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 先在特定群組中查找該LINE用戶
            member = db.query(GroupMember).filter(
                GroupMember.line_user_id == line_user_id,
                GroupMember.group_id == group_id
            ).first()

            if member:
                # 如果找到成員但名稱不同，更新名稱
                if member.name != display_name:
                    logger.info(f"成員 (LINE ID: {line_user_id}) 在群組 {group_id} 的顯示名稱已從 @{member.name} 更新為 @{display_name}。")
                    member.name = display_name
                    member.updated_at = datetime.now(timezone.utc)
                return member

            # 檢查是否有同名成員（無LINE ID）存在於該群組
            existing_member_by_name = db.query(GroupMember).filter(
                GroupMember.name == display_name,
                GroupMember.group_id == group_id,
                GroupMember.line_user_id.is_(None)
            ).first()
            
            if existing_member_by_name:
                logger.info(f"找到現有成員 @{display_name} (ID: {existing_member_by_name.id}) 在群組 {group_id}，更新其 LINE User ID 為 {line_user_id}。")
                existing_member_by_name.line_user_id = line_user_id
                existing_member_by_name.updated_at = datetime.now(timezone.utc)
                if existing_member_by_name.name != display_name:
                    existing_member_by_name.name = display_name
                db.flush()  # 確保更新被持久化
                return existing_member_by_name
            
            # 創建新成員
            logger.info(f"新成員 (LINE ID: {line_user_id}, 名稱: @{display_name}) 在群組 {group_id} 中，將自動建立。")
            member = GroupMember(name=display_name, group_id=group_id, line_user_id=line_user_id)
            db.add(member)
            db.flush()  # 立即獲取ID
            return member
            
        except Exception as e:
            error_msg = str(e).lower()
            if 'unique constraint' in error_msg and attempt < max_retries - 1:
                # 如果是唯一約束錯誤，可能是併發創建，重試查詢
                logger.warning(f"成員創建遇到併發衝突 (嘗試 {attempt + 1}/{max_retries})，重試查詢: {e}")
                db.rollback()
                time.sleep(0.01 * (attempt + 1))  # 短暫延遲後重試
                continue
            else:
                logger.error(f"創建/獲取成員失敗 (嘗試 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                db.rollback()
                time.sleep(0.01 * (attempt + 1))
    
    # 如果所有重試都失敗，拋出異常
    raise Exception(f"無法創建或獲取成員 (LINE ID: {line_user_id}, 群組: {group_id}) 在 {max_retries} 次嘗試後")

def get_or_create_member_by_name(db: Session, name: str, group_id: str) -> GroupMember:
    """
    根據名稱在特定群組中獲取或創建成員
    v1.0 強化：添加重試機制處理競爭條件
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            member = db.query(GroupMember).filter(
                GroupMember.name == name,
                GroupMember.group_id == group_id
            ).first()

            if member:
                return member
            
            # 創建新成員
            logger.info(f"成員 @{name} 在群組 {group_id} 中不存在 (透過名稱查找)，將自動建立 (無 LINE User ID)。")
            member = GroupMember(name=name, group_id=group_id, line_user_id=None)
            db.add(member)
            db.flush()  # 立即獲取ID
            return member
            
        except Exception as e:
            error_msg = str(e).lower()
            if 'unique constraint' in error_msg and attempt < max_retries - 1:
                # 如果是唯一約束錯誤，可能是併發創建，重試查詢
                logger.warning(f"成員創建遇到併發衝突 (嘗試 {attempt + 1}/{max_retries})，重試查詢: {e}")
                db.rollback()
                time.sleep(0.01 * (attempt + 1))  # 短暫延遲後重試
                continue
            else:
                logger.error(f"創建/獲取成員失敗 (嘗試 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                db.rollback()
                time.sleep(0.01 * (attempt + 1))
    
    # 如果所有重試都失敗，拋出異常
    raise Exception(f"無法創建或獲取成員 (名稱: {name}, 群組: {group_id}) 在 {max_retries} 次嘗試後")

def get_bill_by_id(db: Session, bill_id: int, group_id: str) -> Optional[Bill]:
    """獲取特定群組中的帳單"""
    return db.query(Bill).options(
        joinedload(Bill.payer_member_profile),
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
    ).filter(
        Bill.id == bill_id, 
        Bill.group_id == group_id
    ).first()

def get_active_bills_by_group(db: Session, group_id: str) -> List[Bill]:
    """獲取特定群組中的活躍帳單"""
    return db.query(Bill).options(
        joinedload(Bill.payer_member_profile),
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
    ).filter(
        Bill.group_id == group_id, 
        Bill.is_archived == False
    ).order_by(Bill.created_at.desc()).all()



def cleanup_old_duplicate_logs(db: Session, days_to_keep: int = 7):
    """清理舊的重複操作記錄"""
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
    deleted_count = db.query(DuplicatePreventionLog).filter(
        DuplicatePreventionLog.created_at < cutoff_date
    ).delete()
    
    if deleted_count > 0:
        logger.info(f"清理了 {deleted_count} 筆舊的重複操作記錄")
    
    return deleted_count

def generate_content_hash_v284(payer_id: int, description: str, amount: str, participants_str: str, group_id: str) -> str:
    """
    v1.0 強化版內容hash生成：
    - 包含群組ID確保群組隔離
    - 標準化描述（去除多餘空白、統一大小寫）
    - 標準化金額格式
    - 確保參與人排序一致性
    """
    # 標準化描述：去除多餘空白、轉小寫
    normalized_description = ' '.join(description.strip().lower().split())
    
    # 標準化金額：確保格式一致
    from decimal import Decimal
    normalized_amount = str(Decimal(amount).quantize(Decimal('0.01')))
    
    # 標準化參與人：按名稱排序，格式統一
    import re
    mentions = re.findall(r'@(\S+)(?:\s+([\d\.]+))?', participants_str)
    sorted_mentions = sorted(mentions, key=lambda x: x[0].lower())
    
    normalized_participants_parts = []
    for name, amount_str in sorted_mentions:
        if amount_str:
            participant_amount = str(Decimal(amount_str).quantize(Decimal('0.01')))
            normalized_participants_parts.append(f"@{name.lower()}:{participant_amount}")
        else:
            normalized_participants_parts.append(f"@{name.lower()}")
    
    normalized_participants = "|".join(normalized_participants_parts)
    
    # 生成hash：包含所有關鍵資訊
    content = f"{group_id}:{payer_id}:{normalized_description}:{normalized_amount}:{normalized_participants}"
    content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    logger.debug(f"生成content_hash: {content} -> {content_hash}")
    return content_hash

def atomic_create_bill_v284(db: Session, bill_data: dict, participants_data: List[dict]) -> tuple:
    """
    原子性創建帳單 v1.0 強化版：
    - 使用資料庫事務確保一致性
    - 處理重複約束違反
    - 添加重試機制處理併發情況
    - 返回創建結果和狀態
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 在事務開始時再次檢查重複（雙重檢查）
            existing_bill = db.query(Bill).filter(
                Bill.group_id == bill_data['group_id'],
                Bill.content_hash == bill_data['content_hash']
            ).first()
            
            if existing_bill:
                logger.warning(f"事務中發現重複帳單 B-{existing_bill.id} (嘗試 {attempt + 1})")
                # 返回完整的帳單資料
                complete_existing_bill = db.query(Bill).options(
                    joinedload(Bill.payer_member_profile),
                    joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
                ).filter(Bill.id == existing_bill.id).first()
                return complete_existing_bill, "duplicate_found"
            
            # 創建帳單
            new_bill = Bill(**bill_data)
            db.add(new_bill)
            db.flush()  # 獲取新帳單ID但不提交
            
            # 創建參與人記錄
            for participant_data in participants_data:
                participant_data_copy = participant_data.copy()  # 避免修改原始數據
                participant_data_copy['bill_id'] = new_bill.id
                participant = BillParticipant(**participant_data_copy)
                db.add(participant)
            
            # 提交事務
            db.commit()
            
            # 重新查詢完整的帳單資料
            complete_bill = db.query(Bill).options(
                joinedload(Bill.payer_member_profile),
                joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
            ).filter(Bill.id == new_bill.id).first()
            
            logger.info(f"成功創建帳單 B-{new_bill.id} - Hash: {bill_data['content_hash']} (嘗試 {attempt + 1})")
            return complete_bill, "success"
            
        except Exception as e:
            db.rollback()
            error_msg = str(e).lower()
            
            if ('unique constraint' in error_msg or 'duplicate' in error_msg) and 'content_hash' in error_msg:
                logger.warning(f"資料庫唯一約束違反 (嘗試 {attempt + 1}/{max_retries})：{e}")
                
                # 重新查找已存在的重複帳單
                try:
                    existing_bill = db.query(Bill).options(
                        joinedload(Bill.payer_member_profile),
                        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
                    ).filter(
                        Bill.group_id == bill_data['group_id'],
                        Bill.content_hash == bill_data['content_hash']
                    ).first()
                    
                    if existing_bill:
                        logger.info(f"找到已存在的重複帳單 B-{existing_bill.id}")
                        return existing_bill, "duplicate_constraint"
                except Exception as query_error:
                    logger.error(f"查詢重複帳單時發生錯誤：{query_error}")
                
                return None, "constraint_error"
                
            elif attempt < max_retries - 1:
                # 其他錯誤且還有重試機會
                logger.warning(f"創建帳單遇到錯誤 (嘗試 {attempt + 1}/{max_retries})，將重試：{e}")
                time.sleep(0.01 * (attempt + 1))  # 短暫延遲後重試
                continue
            else:
                # 最後一次嘗試失敗
                logger.exception(f"創建帳單時發生未預期錯誤 (最終嘗試)：{e}")
                return None, "unexpected_error"
    
    # 如果所有重試都失敗
    logger.error(f"無法創建帳單在 {max_retries} 次嘗試後")
    return None, "max_retries_exceeded"
