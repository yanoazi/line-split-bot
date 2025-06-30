# models_splitbill.py (v2.7.0 - LINE User ID for Payer, Simplified Commands)
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
    UniqueConstraint, Boolean, Numeric, Enum as SQLAEnum
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session, joinedload
from typing import Optional, List
from sqlalchemy.sql import func
from contextlib import contextmanager
from datetime import datetime
import enum

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

class SplitType(enum.Enum):
    EQUAL = "equal"
    UNEQUAL = "unequal" # Represents specific amounts per participant

class GroupMember(Base):
    __tablename__ = "sb_group_members"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True) # @mention name in the group
    group_id = Column(String, nullable=False, index=True) # LINE Group ID
    # LINE User ID, unique if present. Crucial for identifying payers. Optional for other participants initially.
    line_user_id = Column(String, index=True, nullable=True, unique=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    paid_bills = relationship("Bill", back_populates="payer_member_profile", foreign_keys="[Bill.payer_member_id]")
    bill_participations = relationship("BillParticipant", back_populates="debtor_member_profile", foreign_keys="[BillParticipant.debtor_member_id]")

    __table_args__ = (
        UniqueConstraint('name', 'group_id', name='_sb_member_name_group_uc'),
        # UniqueConstraint('line_user_id', 'group_id', name='_sb_line_user_group_uc'), # A user can only have one name per group via line_user_id
        # The line_user_id itself being unique globally (if not null) is handled by `unique=True` on the column.
    )

    def __repr__(self):
        return f"<GroupMember(id={self.id}, name='{self.name}', group_id='{self.group_id}', line_id='{self.line_user_id}')>"

class Bill(Base):
    __tablename__ = "sb_bills"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=False)
    # This is the grand total amount of the bill that the payer paid.
    total_bill_amount = Column(Numeric(10, 2), nullable=False) 

    # Foreign key to GroupMember table for the payer
    payer_member_id = Column(Integer, ForeignKey('sb_group_members.id'), nullable=False)
    # Relationship to the GroupMember who paid
    payer_member_profile = relationship("GroupMember", back_populates="paid_bills", foreign_keys=[payer_member_id])

    split_type = Column(SQLAEnum(SplitType, name="sb_split_type_enum"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_archived = Column(Boolean, default=False, nullable=False)

    participants = relationship("BillParticipant", back_populates="bill", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Bill(id={self.id}, group='{self.group_id}', desc='{self.description[:20]}', amount={self.total_bill_amount}, payer_id={self.payer_member_id})>"

class BillParticipant(Base):
    __tablename__ = "sb_bill_participants"
    id = Column(Integer, primary_key=True, index=True)

    bill_id = Column(Integer, ForeignKey('sb_bills.id'), nullable=False)
    bill = relationship("Bill", back_populates="participants")

    # Foreign key to GroupMember table for the debtor
    debtor_member_id = Column(Integer, ForeignKey('sb_group_members.id'), nullable=False)
    # Relationship to the GroupMember who owes money
    debtor_member_profile = relationship("GroupMember", back_populates="bill_participations", foreign_keys=[debtor_member_id])

    amount_owed = Column(Numeric(10, 2), nullable=False) # Amount this participant owes to the payer for this bill
    is_paid = Column(Boolean, default=False, nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint('bill_id', 'debtor_member_id', name='_sb_bill_debtor_uc'),)

    def __repr__(self):
        return f"<BillParticipant(bill_id={self.bill_id}, debtor_id={self.debtor_member_id}, owed={self.amount_owed}, paid={self.is_paid})>"

def init_db_splitbill():
    logger.info("初始化分帳資料庫 (v2.7.0 - LINE User ID for Payer)，嘗試建立表格...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("分帳表格建立完成 (如果原本不存在的話)。")
    except Exception as e:
        logger.exception(f"初始化分帳資料庫時發生錯誤: {e}")

def get_or_create_member_by_line_id(db: Session, line_user_id: str, group_id: str, display_name: str) -> GroupMember:
    """
    Gets or creates a member primarily by their LINE User ID and group_id.
    Updates display_name if it has changed or if member is new.
    """
    member = db.query(GroupMember).filter(
        GroupMember.line_user_id == line_user_id,
        GroupMember.group_id == group_id
    ).first()

    if not member:
        # Check if a member with this display_name already exists in the group (e.g., added by mention before line_id known)
        existing_member_by_name = db.query(GroupMember).filter(
            GroupMember.name == display_name,
            GroupMember.group_id == group_id,
            GroupMember.line_user_id == None # Only if their line_id is not yet known
        ).first()
        if existing_member_by_name:
            logger.info(f"找到現有成員 @{display_name} (ID: {existing_member_by_name.id})，更新其 LINE User ID 為 {line_user_id}。")
            existing_member_by_name.line_user_id = line_user_id
            if existing_member_by_name.name != display_name : # Should not happen if fetched by name, but for consistency
                existing_member_by_name.name = display_name # Update name if it changed
            member = existing_member_by_name
        else:
            logger.info(f"新成員 (LINE ID: {line_user_id}, 名稱: @{display_name}) 在群組 {group_id} 中，將自動建立。")
            member = GroupMember(name=display_name, group_id=group_id, line_user_id=line_user_id)
            db.add(member)
    elif member.name != display_name: # Existing member found by line_user_id, but display name changed
        logger.info(f"成員 (LINE ID: {line_user_id}) 的顯示名稱已從 @{member.name} 更新為 @{display_name}。")
        member.name = display_name
    return member

def get_or_create_member_by_name(db: Session, name: str, group_id: str) -> GroupMember:
    """
    Gets or creates a member by their @mention name and group_id.
    LINE User ID will be unknown for members created this way, until they interact.
    """
    member = db.query(GroupMember).filter(
        GroupMember.name == name,
        GroupMember.group_id == group_id
    ).first()

    if not member:
        logger.info(f"成員 @{name} 在群組 {group_id} 中不存在 (透過名稱查找)，將自動建立 (無 LINE User ID)。")
        member = GroupMember(name=name, group_id=group_id, line_user_id=None) # line_user_id is initially None
        db.add(member)
    return member


def get_bill_by_id(db: Session, bill_id: int, group_id: str) -> Optional[Bill]:
    return db.query(Bill).options(
        joinedload(Bill.payer_member_profile), # Updated relationship name
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile) # Updated relationship name
    ).filter(Bill.id == bill_id, Bill.group_id == group_id).first()

def get_active_bills_by_group(db: Session, group_id: str) -> List[Bill]:
    return db.query(Bill).options(
        joinedload(Bill.payer_member_profile),
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member_profile)
    ).filter(Bill.group_id == group_id, Bill.is_archived == False)\
     .order_by(Bill.created_at.desc()).all()

def get_unpaid_debts_for_member_by_line_id(db: Session, line_user_id: str, group_id: str) -> List[BillParticipant]:
    """
    Fetches unpaid debts for a member identified by their LINE User ID within a specific group.
    """
    # First, find the member_id for this line_user_id in this group
    member = db.query(GroupMember.id).filter(
        GroupMember.line_user_id == line_user_id,
        GroupMember.group_id == group_id
    ).scalar_one_or_none()

    if not member:
        return [] # No member found for this line_user_id in this group

    member_id = member # member is actually the ID here due to .scalar_one_or_none() on GroupMember.id

    return db.query(BillParticipant)\
        .join(BillParticipant.bill)\
        .options(
            joinedload(BillParticipant.bill).joinedload(Bill.payer_member_profile),
        )\
        .filter(
            BillParticipant.debtor_member_id == member_id,
            BillParticipant.is_paid == False,
            Bill.group_id == group_id, 
            Bill.is_archived == False
        )\
        .order_by(Bill.created_at.asc())\
        .all()