# models_splitbill.py
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
import enum # For Python enums

from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv() # Ensure .env is loaded if you use it for DATABASE_URL

DATABASE_URL = os.environ.get('DATABASE_URL_SPLITBILL', os.environ.get('DATABASE_URL')) # Use a specific or fallback
if not DATABASE_URL:
    raise ValueError("環境變數 DATABASE_URL_SPLITBILL 或 DATABASE_URL 未設定！")

if DATABASE_URL.startswith("postgres://"): # Render compatibility
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

@contextmanager
def get_db_splitbill(): # Renamed for clarity if used alongside other DBs
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
    UNEQUAL = "unequal"
    # PERCENTAGE = "percentage" # Future consideration

class GroupMember(Base):
    __tablename__ = "sb_group_members" # sb prefix for split_bill
    id = Column(Integer, primary_key=True, index=True)
    # The @mention name, unique per group
    name = Column(String, nullable=False, index=True)
    group_id = Column(String, nullable=False, index=True) # LINE Group ID
    # Optional: Store actual LINE User ID if member interacts or is registered
    line_user_id = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    paid_bills = relationship("Bill", back_populates="payer", foreign_keys="[Bill.payer_id]")
    bill_participations = relationship("BillParticipant", back_populates="debtor_member", foreign_keys="[BillParticipant.debtor_id]")

    __table_args__ = (UniqueConstraint('name', 'group_id', name='_sb_member_name_group_uc'),)

    def __repr__(self):
        return f"<GroupMember(id={self.id}, name='{self.name}', group_id='{self.group_id}')>"

class Bill(Base):
    __tablename__ = "sb_bills"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(String, nullable=False, index=True) # LINE Group ID
    description = Column(Text, nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False) # e.g., 12345.67

    payer_id = Column(Integer, ForeignKey('sb_group_members.id'), nullable=False)
    payer = relationship("GroupMember", back_populates="paid_bills", foreign_keys=[payer_id])

    split_type = Column(SQLAEnum(SplitType, name="sb_split_type_enum"), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Optional: A bill could be considered 'fully_settled' if all participants have paid
    is_archived = Column(Boolean, default=False, nullable=False) # To hide old/settled bills

    participants = relationship("BillParticipant", back_populates="bill", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Bill(id={self.id}, group='{self.group_id}', desc='{self.description[:20]}', amount={self.total_amount}, payer_id={self.payer_id})>"

class BillParticipant(Base):
    __tablename__ = "sb_bill_participants"
    id = Column(Integer, primary_key=True, index=True)

    bill_id = Column(Integer, ForeignKey('sb_bills.id'), nullable=False)
    bill = relationship("Bill", back_populates="participants")

    debtor_id = Column(Integer, ForeignKey('sb_group_members.id'), nullable=False)
    debtor_member = relationship("GroupMember", back_populates="bill_participations", foreign_keys=[debtor_id])

    amount_owed = Column(Numeric(10, 2), nullable=False)
    is_paid = Column(Boolean, default=False, nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint('bill_id', 'debtor_id', name='_sb_bill_debtor_uc'),
        {'extend_existing': True}
    )

    def __repr__(self):
        return f"<BillParticipant(bill_id={self.bill_id}, debtor_id={self.debtor_id}, owed={self.amount_owed}, paid={self.is_paid})>"

def init_db_splitbill(): # Renamed
    logger.info("初始化分帳資料庫，嘗試建立表格...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("分帳表格建立完成 (如果原本不存在的話)。")
    except Exception as e:
        logger.exception(f"初始化分帳資料庫時發生錯誤: {e}")

def get_or_create_member(db: Session, name: str, group_id: str, line_user_id: Optional[str] = None) -> GroupMember:
    member = db.query(GroupMember).filter_by(name=name, group_id=group_id).first()
    if not member:
        member = GroupMember(name=name, group_id=group_id, line_user_id=line_user_id)
        db.add(member)
        logger.info(f"準備建立成員: {name} in group {group_id}")
    elif line_user_id and not member.line_user_id:
        member.line_user_id = line_user_id
        logger.info(f"更新成員 {name} 的 line_user_id")
    return member

def get_bill_by_id(db: Session, bill_id: int, group_id: str) -> Optional[Bill]:
    return db.query(Bill).options(
        joinedload(Bill.payer),
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member)
    ).filter(Bill.id == bill_id, Bill.group_id == group_id).first()

def get_active_bills_by_group(db: Session, group_id: str) -> List[Bill]:
    return db.query(Bill).options(
        joinedload(Bill.payer),
        joinedload(Bill.participants).joinedload(BillParticipant.debtor_member)
    ).filter(Bill.group_id == group_id, Bill.is_archived == False)\
     .order_by(Bill.created_at.desc()).all()

def get_unpaid_debts_for_member(db: Session, member_id: int, group_id: str) -> List[BillParticipant]:
    return db.query(BillParticipant)\
        .join(BillParticipant.bill)\
        .options(
            joinedload(BillParticipant.bill).joinedload(Bill.payer)
        )\
        .filter(
            BillParticipant.debtor_id == member_id,
            BillParticipant.is_paid == False,
            Bill.group_id == group_id,
            Bill.is_archived == False
        )\
        .order_by(Bill.created_at.asc())\
        .all()