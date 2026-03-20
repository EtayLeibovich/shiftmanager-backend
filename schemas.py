from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel


# ==========================================
# Business
# ==========================================
class BusinessCreate(BaseModel):
    name: str


class BusinessResponse(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


# ==========================================
# Branch
# ==========================================
class BranchCreate(BaseModel):
    business_id: int
    name: str
    address: Optional[str] = ""


class BranchResponse(BaseModel):
    id: int
    business_id: int
    name: str
    address: Optional[str] = ""

    class Config:
        from_attributes = True


# ==========================================
# User / Employee
# ==========================================
class UserCreate(BaseModel):
    business_id: int
    full_name: str
    role: str = "worker"
    passcode: str
    hourly_rate: float = 0.0
    phone: Optional[str] = None
    email: Optional[str] = None
    branch_id: Optional[int] = None


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    passcode: Optional[str] = None
    hourly_rate: Optional[float] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    branch_id: Optional[int] = None
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: int
    business_id: int
    full_name: str
    role: str
    hourly_rate: Optional[float] = 0.0
    phone: Optional[str] = None
    email: Optional[str] = None
    branch_id: Optional[int] = None
    is_active: Optional[bool] = True

    class Config:
        from_attributes = True


# ==========================================
# Shift
# ==========================================
class ShiftAction(BaseModel):
    passcode: str


class ShiftResponse(BaseModel):
    id: int
    user_id: int
    clock_in: datetime
    clock_out: Optional[datetime] = None
    total_hours: Optional[float] = None

    class Config:
        from_attributes = True


class ShiftRead(BaseModel):
    id: int
    full_name: str
    clock_in: datetime
    clock_out: Optional[datetime]
    total_hours: Optional[float]

    class Config:
        from_attributes = True


# ==========================================
# Scheduled Shift
# ==========================================
class ScheduledShiftCreate(BaseModel):
    user_id: int
    business_id: int
    start_time: datetime
    end_time: datetime
    notes: Optional[str] = ""


class ScheduledShiftResponse(BaseModel):
    id: int
    user_id: int
    business_id: int
    start_time: datetime
    end_time: datetime
    notes: Optional[str] = ""

    class Config:
        from_attributes = True


# ==========================================
# Leave Request
# ==========================================
class LeaveRequestCreate(BaseModel):
    passcode: str
    leave_type: str = "vacation"
    start_date: date
    end_date: date
    reason: Optional[str] = ""


class LeaveStatusUpdate(BaseModel):
    status: str
    manager_note: Optional[str] = ""


class LeaveRequestResponse(BaseModel):
    id: int
    user_id: int
    business_id: int
    leave_type: str
    start_date: date
    end_date: date
    reason: Optional[str] = ""
    status: str
    manager_note: Optional[str] = ""
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ==========================================
# Notification
# ==========================================
class NotificationResponse(BaseModel):
    id: int
    business_id: int
    user_id: Optional[int] = None
    type: str
    message: str
    is_read: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ==========================================
# Auth
# ==========================================
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class TwoFactorRequiredResponse(BaseModel):
    """Returned by /auth/token when the user has 2FA enabled."""
    requires_2fa: bool = True
    pending_token: str   # short-lived JWT (5 min), scope='2fa_pending'


class TwoFactorVerifyRequest(BaseModel):
    """Body for /auth/verify-2fa — covers both login and setup-confirmation flows."""
    pending_token: str   # the short-lived JWT returned by /auth/token or /auth/setup-2fa
    code: str            # 6-digit TOTP code from Google Authenticator


class TwoFactorSetupResponse(BaseModel):
    """Returned by /auth/setup-2fa."""
    provisioning_uri: str   # otpauth:// URI — encode this as a QR code in the frontend
    secret: str             # base32 secret — show as fallback for manual entry
    setup_token: str        # short-lived JWT (5 min), scope='2fa_setup' — use in verify-2fa


# ==========================================
# Reports
# ==========================================
class SalaryReportRow(BaseModel):
    user_id: int
    full_name: str
    role: str
    total_shifts: int
    total_hours: float
    hourly_rate: float
    estimated_salary: float


class MonthlyReport(BaseModel):
    year: int
    month: int
    employees: List[SalaryReportRow]
    grand_total_hours: float
    grand_total_salary: float
