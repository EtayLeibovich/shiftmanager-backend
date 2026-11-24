from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Date, Boolean, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    join_code = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    users = relationship("User", back_populates="business")
    branches = relationship("Branch", back_populates="business")
    scheduled_shifts = relationship("ScheduledShift", back_populates="business")
    leave_requests = relationship("LeaveRequest", back_populates="business")
    notifications = relationship("Notification", back_populates="business")


class Branch(Base):
    __tablename__ = "branches"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"))
    name = Column(String, index=True)
    address = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="branches")
    users = relationship("User", back_populates="branch")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"))
    branch_id = Column(Integer, ForeignKey("branches.id"), nullable=True)
    full_name = Column(String, index=True)
    role = Column(String, default="worker")
    passcode = Column(String)             # plaintext PIN — used as unique lookup identifier
    passcode_hash = Column(String, nullable=True)  # bcrypt hash — used for secure auth verification
    hourly_rate = Column(Float, default=0.0, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    is_active = Column(Boolean, default=True, nullable=True)
    approval_status = Column(String, default="approved", nullable=True)  # 'pending' | 'approved' | 'rejected'
    totp_secret = Column(String, nullable=True)
    is_2fa_enabled = Column(Integer, default=0, nullable=True)
    birth_date = Column(Date, nullable=True)  # לבדיקות חוק עבודת הנוער

    business = relationship("Business", back_populates="users")
    branch = relationship("Branch", back_populates="users", foreign_keys=[branch_id])
    shifts = relationship("Shift", back_populates="user")
    scheduled_shifts = relationship("ScheduledShift", back_populates="user")
    leave_requests = relationship("LeaveRequest", back_populates="user")
    notifications = relationship("Notification", back_populates="user")


class ShiftPreference(Base):
    """העדפת משמרת של עובד לימים שעדיין לא סוידרו"""
    __tablename__ = "shift_preferences"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    week_start = Column(Date)
    day_of_week = Column(Integer)   # 0=ראשון ... 6=שבת
    preference = Column(String)     # 'morning' | 'evening' | 'double'

    business = relationship("Business")
    user = relationship("User")


class ShiftRoster(Base):
    """סידור עבודה שבועי — מנהל קובע לכל עובד משמרת לכל יום"""
    __tablename__ = "shift_roster"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    week_start = Column(Date)           # תאריך ראשון של השבוע (ראשון)
    day_of_week = Column(Integer)       # 0=ראשון, 1=שני, ..., 6=שבת
    shift_type = Column(String)         # 'morning' | 'evening' | 'double' | 'off'
    position = Column(String, nullable=True)   # 'מטבח' | 'פס' | 'גריל'
    note = Column(Text, nullable=True)

    business = relationship("Business")
    user = relationship("User")


class Shift(Base):
    __tablename__ = "shifts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    clock_in = Column(DateTime(timezone=True), server_default=func.now())
    clock_out = Column(DateTime(timezone=True), nullable=True)
    total_hours = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)

    user = relationship("User", back_populates="shifts")


class ScheduledShift(Base):
    __tablename__ = "scheduled_shifts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    business_id = Column(Integer, ForeignKey("businesses.id"))
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    notes = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="scheduled_shifts")
    business = relationship("Business", back_populates="scheduled_shifts")


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    business_id = Column(Integer, ForeignKey("businesses.id"))
    leave_type = Column(String, default="vacation")
    start_date = Column(Date)
    end_date = Column(Date)
    reason = Column(Text, default="")
    status = Column(String, default="pending")
    manager_note = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="leave_requests")
    business = relationship("Business", back_populates="leave_requests")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    type = Column(String)
    message = Column(Text)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="notifications")
    user = relationship("User", back_populates="notifications")


class ShiftSwap(Base):
    """בקשת מחליף: עובד (או המנהל) מפרסם משמרת, עובדים מתנדבים, המנהל מאשר אחד."""
    __tablename__ = "shift_swaps"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), index=True)
    requester_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # None = פורסם ע"י המנהל
    date = Column(Date)
    shift_type = Column(String)            # morning | evening | double
    note = Column(Text, nullable=True)
    status = Column(String, default="open")  # open | filled | cancelled
    filled_by = Column(Integer, nullable=True)
    created_at = Column(DateTime)
    resolved_at = Column(DateTime, nullable=True)


class SwapVolunteer(Base):
    __tablename__ = "swap_volunteers"

    id = Column(Integer, primary_key=True, index=True)
    swap_id = Column(Integer, ForeignKey("shift_swaps.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String, default="pending")  # pending | approved | declined
    created_at = Column(DateTime)


class AuditLog(Base):
    """יומן שינויים — כל רשומה נושאת hash של הקודמת, כך ששינוי מחוץ למערכת מתגלה."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), index=True)
    actor_id = Column(Integer, nullable=True)
    actor_name = Column(String, nullable=True)
    action = Column(String)              # e.g. shift.edit, roster.save, leave.approved
    entity = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    summary = Column(Text)
    before = Column(Text, nullable=True)   # JSON
    after = Column(Text, nullable=True)    # JSON
    source = Column(String, default="manual")  # manual | ai | system
    created_at = Column(DateTime)          # naive Israel time, seconds precision (part of the hash)
    prev_hash = Column(String, default="")
    hash = Column(String)
