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
    totp_secret = Column(String, nullable=True)          # TOTP secret key (encrypted at rest via DB-level AES in prod)
    is_2fa_enabled = Column(Boolean, default=False, nullable=True)

    business = relationship("Business", back_populates="users")
    branch = relationship("Branch", back_populates="users", foreign_keys=[branch_id])
    shifts = relationship("Shift", back_populates="user")
    scheduled_shifts = relationship("ScheduledShift", back_populates="user")
    leave_requests = relationship("LeaveRequest", back_populates="user")
    notifications = relationship("Notification", back_populates="user")


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
