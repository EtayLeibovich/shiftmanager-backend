from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, extract, text
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel
import models
import schemas
import auth
from database import engine, get_db
from groq import Groq
from passlib.context import CryptContext
import csv
import io
import random
import string
import os
import pyotp
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# SECURITY — bcrypt password hashing context
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
_groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def now_israel():
    return datetime.now(ISRAEL_TZ).replace(tzinfo=None)


def to_naive(dt):
    """Strip timezone info so arithmetic works whether DB returns aware or naive datetimes."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


# ---------------------------------------------------------------------------
# SECURITY — passcode hashing helpers
# ---------------------------------------------------------------------------
def hash_passcode(plain: str) -> str:
    """Hash a plaintext passcode with bcrypt before storing it in the DB."""
    return pwd_context.hash(plain)


def calc_shift_salary(hours: float, hourly_rate: float) -> float:
    """
    Israeli labor law overtime calculation per shift:
      - Hours 1–8  : 100% (regular)
      - Hours 8–10 : 125% (rishon / shniyah)
      - Hours 10+  : 150% (shilishit+)
    """
    if (hourly_rate or 0) <= 0 or (hours or 0) <= 0:
        return 0.0
    rate = hourly_rate
    regular      = min(hours, 8.0)          * rate * 1.00
    overtime_125 = max(0.0, min(hours, 10.0) - 8.0)  * rate * 1.25
    overtime_150 = max(0.0, hours - 10.0)   * rate * 1.50
    return round(regular + overtime_125 + overtime_150, 2)


def verify_passcode(plain: str, user: models.User, db: Session) -> bool:
    """
    Verify a passcode against the stored bcrypt hash.
    Lazy-migration: if the hash doesn't exist yet (legacy user), accept the
    plaintext match and immediately upgrade the record to a bcrypt hash.
    This ensures a smooth rollout without forcing a full DB reset.
    """
    if user.passcode_hash:
        return pwd_context.verify(plain, user.passcode_hash)
    # Legacy user — no hash stored yet. Accept plaintext and upgrade.
    if user.passcode == plain:
        user.passcode_hash = pwd_context.hash(plain)
        db.commit()
        return True
    return False


def get_current_user(authorization: str = Header(None), db: Session = Depends(get_db)) -> models.User:
    """
    FastAPI dependency: extracts and validates the Bearer JWT from the Authorization header.
    Explicitly rejects 2FA pending/setup tokens — only full access tokens are accepted here.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="לא מורשה — נדרש טוקן")
    token = authorization.split(" ", 1)[1]
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="טוקן לא תקף או פג תוקף")
    if payload.get("scope") in ("2fa_pending", "2fa_setup"):
        raise HTTPException(status_code=401, detail="נדרש אימות 2FA להשלמת ההתחברות")
    user = db.query(models.User).filter(models.User.id == payload["user_id"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="משתמש לא נמצא")
    return user


def get_setup_user(authorization: str = Header(None), db: Session = Depends(get_db)) -> models.User:
    """
    Like get_current_user but also accepts 'setup_required' tokens —
    used only by /auth/setup-2fa so a manager can set up 2FA right after first login.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="לא מורשה — נדרש טוקן")
    token = authorization.split(" ", 1)[1]
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="טוקן לא תקף או פג תוקף")
    if payload.get("scope") in ("2fa_pending", "2fa_setup"):
        raise HTTPException(status_code=401, detail="טוקן לא תקף לפעולה זו")
    user = db.query(models.User).filter(models.User.id == payload["user_id"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="משתמש לא נמצא")
    return user


def run_migrations():
    migrations = [
        "ALTER TABLE users ADD COLUMN hourly_rate REAL DEFAULT 0.0",
        "ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN branch_id INTEGER",
        "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
        "ALTER TABLE users ADD COLUMN passcode_hash TEXT",
        "ALTER TABLE users ADD COLUMN approval_status TEXT DEFAULT 'approved'",
        "ALTER TABLE users ADD COLUMN totp_secret TEXT",
        "ALTER TABLE users ADD COLUMN is_2fa_enabled INTEGER DEFAULT 0",
    ]
    for m in migrations:
        # כל migration רץ בחיבור נפרד — חיוני ב-PostgreSQL שבו שגיאה
        # סוגרת את כל הטרנזקציה הנוכחית ומונעת הרצת פקודות נוספות
        with engine.connect() as conn:
            try:
                conn.execute(text(m))
                conn.commit()
            except Exception:
                conn.rollback()  # חובה ב-PostgreSQL לנקות את הטרנזקציה השגויה


models.Base.metadata.create_all(bind=engine)
run_migrations()


def auto_seed():
    """יצירת עסק ומנהל ברירת מחדל אוטומטית אם לא קיימים"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        biz = db.query(models.Business).filter(models.Business.name == "העסק שלי").first()
        if not biz:
            biz = models.Business(name="העסק שלי", join_code="ADMIN1")
            db.add(biz)
            db.commit()
            db.refresh(biz)
        manager = db.query(models.User).filter(
            models.User.business_id == biz.id,
            models.User.role == "manager"
        ).first()
        if not manager:
            default_passcode = "1234"
            manager = models.User(
                business_id=biz.id,
                full_name="מנהל ראשי",
                role="manager",
                passcode=default_passcode,
                # SECURITY: store bcrypt hash — never authenticate against plaintext in DB
                passcode_hash=hash_passcode(default_passcode),
                is_active=True,
                approval_status="approved",
            )
            db.add(manager)
            db.commit()
        elif manager.passcode_hash is None:
            # Lazy migration: upgrade existing manager to bcrypt hash
            manager.passcode_hash = hash_passcode(manager.passcode)
            db.commit()
    finally:
        db.close()


auto_seed()

app = FastAPI(title="ShiftManager SaaS API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "https://shiftmanager-frontend.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


class ShiftEditRequest(BaseModel):
    clock_in: datetime
    clock_out: datetime


class ManualShiftOpen(BaseModel):
    user_id: int
    clock_in: datetime


class ManualShiftClose(BaseModel):
    clock_out: datetime


def get_manager_payload(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token missing")
    token = authorization.split(" ")[1]
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("role") != "manager":
        raise HTTPException(status_code=403, detail="Manager access required")
    return payload


def create_notification(db: Session, business_id: int, type: str, message: str, user_id: int = None):
    notif = models.Notification(
        business_id=business_id,
        user_id=user_id,
        type=type,
        message=message,
        is_read=False,
    )
    db.add(notif)
    db.commit()


@app.get("/")
def read_root():
    return {"message": "Welcome to ShiftManager API!"}


@app.post("/setup/init")
def setup_init(db: Session = Depends(get_db)):
    """יצירת עסק ומנהל ראשוני אם לא קיימים"""
    biz = db.query(models.Business).filter(models.Business.name == "העסק שלי").first()
    if not biz:
        biz = models.Business(name="העסק שלי", join_code="ADMIN1")
        db.add(biz)
        db.commit()
        db.refresh(biz)

    manager = db.query(models.User).filter(
        models.User.business_id == biz.id,
        models.User.role == "manager"
    ).first()
    if not manager:
        default_passcode = "1234"
        manager = models.User(
            business_id=biz.id,
            full_name="מנהל ראשי",
            role="manager",
            passcode=default_passcode,
            passcode_hash=hash_passcode(default_passcode),
            is_active=True,
            approval_status="approved",
        )
        db.add(manager)
        db.commit()
        db.refresh(manager)

    return {
        "status": "ok",
        "business_id": biz.id,
        "business_name": biz.name,
        "join_code": biz.join_code,
        # SECURITY: passcode not returned in API response
    }


# ==========================================
# AUTH
# ==========================================
class RegisterRequest(BaseModel):
    join_code: str
    full_name: str
    passcode: str
    phone: str = ""
    email: str = ""


@app.post("/businesses/{join_code}/join-code-check")
def check_join_code(join_code: str, db: Session = Depends(get_db)):
    biz = db.query(models.Business).filter(models.Business.join_code == join_code.upper()).first()
    if not biz:
        raise HTTPException(status_code=404, detail="קוד לא נמצא")
    return {"business_name": biz.name}


@app.post("/register")
def register_employee(req: RegisterRequest, db: Session = Depends(get_db)):
    biz = db.query(models.Business).filter(models.Business.join_code == req.join_code.upper()).first()
    if not biz:
        raise HTTPException(status_code=404, detail="קוד עסק לא נמצא")
    # בדוק שהקוד האישי לא תפוס
    existing = db.query(models.User).filter(
        models.User.passcode == req.passcode,
        models.User.business_id == biz.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="קוד אישי תפוס, בחר קוד אחר")
    new_user = models.User(
        business_id=biz.id,
        full_name=req.full_name,
        role="worker",
        passcode=req.passcode,
        passcode_hash=hash_passcode(req.passcode),  # SECURITY: store bcrypt hash
        phone=req.phone,
        email=req.email,
        is_active=False,
        approval_status="pending",
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    create_notification(
        db, biz.id, "registration_pending",
        f"🆕 {req.full_name} מבקש להצטרף לעסק - ממתין לאישורך"
    )
    return {"message": "הבקשה נשלחה! המנהל יאשר אותך בקרוב.", "user_id": new_user.id}


@app.get("/register/pending/{business_id}")
def get_pending_registrations(business_id: int, db: Session = Depends(get_db)):
    users = db.query(models.User).filter(
        models.User.business_id == business_id,
        models.User.approval_status == "pending"
    ).all()
    # SECURITY: passcode_hash is never returned; passcode returned only for manager workflow
    return [{"id": u.id, "full_name": u.full_name, "phone": u.phone, "email": u.email} for u in users]


@app.put("/register/{user_id}/approve")
def approve_registration(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    user.approval_status = "approved"
    user.is_active = True
    db.commit()
    create_notification(
        db, user.business_id, "registration_approved",
        f"✅ בקשת ההצטרפות שלך אושרה! ברוך הבא {user.full_name}",
        user_id=user.id
    )
    return {"message": "אושר"}


@app.put("/register/{user_id}/reject")
def reject_registration(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    user.approval_status = "rejected"
    user.is_active = False
    db.commit()
    return {"message": "נדחה"}


@app.post("/auth/token")
def login_token(action: schemas.ShiftAction, db: Session = Depends(get_db)):
    # Look up by passcode identifier, then verify via bcrypt hash
    user = db.query(models.User).filter(models.User.passcode == action.passcode).first()
    # SECURITY: use constant-time hash verification to prevent timing attacks
    if not user or not verify_passcode(action.passcode, user, db):
        raise HTTPException(status_code=401, detail="קוד שגוי")
    if user.approval_status == "pending":
        raise HTTPException(status_code=403, detail="החשבון ממתין לאישור המנהל")
    if user.approval_status == "rejected":
        raise HTTPException(status_code=403, detail="הבקשה נדחתה על ידי המנהל")

    # --- 2FA gate (managers only) ---
    if user.role == "manager":
        if user.is_2fa_enabled and user.totp_secret:
            # 2FA active → challenge with 6-digit code
            pending_token = auth.create_pending_token({"user_id": user.id, "scope": "2fa_pending"})
            return {"requires_2fa": True, "pending_token": pending_token}
        else:
            # 2FA not set up yet → force mandatory setup before entering admin
            pending_token = auth.create_pending_token({"user_id": user.id, "scope": "setup_required"})
            return {"requires_2fa_setup": True, "pending_token": pending_token}

    token = auth.create_access_token({
        "user_id": user.id,
        "business_id": user.business_id,
        "role": user.role
    })
    return {"access_token": token, "token_type": "bearer", "user": user}


@app.post("/auth/setup-2fa", response_model=schemas.TwoFactorSetupResponse)
def setup_2fa(current_user: models.User = Depends(get_setup_user), db: Session = Depends(get_db)):
    """
    Generates a new TOTP secret for the authenticated user and returns:
    - provisioning_uri: encode as QR code in the frontend
    - secret: base32 string for manual entry into Google Authenticator
    - setup_token: short-lived JWT to be passed to /auth/verify-2fa to confirm setup

    NOTE: is_2fa_enabled is NOT set to True here. The user must first scan the QR code
    and verify a valid code via /auth/verify-2fa to activate 2FA. This prevents lockout
    if the user abandons the setup flow midway.
    """
    secret = pyotp.random_base32()
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(
        name=current_user.full_name,
        issuer_name="ShiftManager"
    )
    # Persist the new secret (not yet enabled) so verify-2fa can read it
    current_user.totp_secret = secret
    current_user.is_2fa_enabled = False  # only activated after successful verify
    db.commit()

    setup_token = auth.create_pending_token({
        "user_id": current_user.id,
        "scope": "2fa_setup"
    })
    return {
        "provisioning_uri": provisioning_uri,
        "secret": secret,
        "setup_token": setup_token
    }


@app.post("/auth/verify-2fa")
def verify_2fa(body: schemas.TwoFactorVerifyRequest, db: Session = Depends(get_db)):
    """
    Dual-purpose endpoint — handles two flows based on the pending token's scope:

    1. scope='2fa_pending'  (login flow):
       Verifies the TOTP code, then issues the full JWT access token.

    2. scope='2fa_setup'  (setup activation flow):
       Verifies the TOTP code against the newly generated secret,
       then sets is_2fa_enabled=True to complete the setup.

    The TOTP window is ±1 step (±30 seconds) to allow for minor clock drift.
    """
    payload = auth.verify_token(body.pending_token)
    if not payload or payload.get("scope") not in ("2fa_pending", "2fa_setup"):
        raise HTTPException(status_code=401, detail="טוקן לא תקף או פג תוקף")

    user = db.query(models.User).filter(models.User.id == payload["user_id"]).first()
    if not user or not user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA לא מוגדר לחשבון זה")

    totp = pyotp.TOTP(user.totp_secret)
    # valid_window=1 allows ±30 sec clock drift (one time step in each direction)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status_code=401, detail="קוד שגוי — נסה שוב")

    scope = payload["scope"]

    if scope == "2fa_setup":
        # Activation: code is correct → enable 2FA and issue full JWT so user enters directly
        user.is_2fa_enabled = True
        db.commit()
        token = auth.create_access_token({
            "user_id": user.id,
            "business_id": user.business_id,
            "role": user.role
        })
        return {"access_token": token, "token_type": "bearer", "setup_complete": True, "user": {
            "id": user.id, "business_id": user.business_id, "full_name": user.full_name,
            "role": user.role, "hourly_rate": user.hourly_rate, "phone": user.phone,
            "email": user.email, "branch_id": user.branch_id, "is_active": user.is_active,
        }}

    # Login flow: issue the full access token
    token = auth.create_access_token({
        "user_id": user.id,
        "business_id": user.business_id,
        "role": user.role
    })
    return {"access_token": token, "token_type": "bearer", "user": {
        "id": user.id,
        "business_id": user.business_id,
        "full_name": user.full_name,
        "role": user.role,
        "hourly_rate": user.hourly_rate,
        "phone": user.phone,
        "email": user.email,
        "branch_id": user.branch_id,
        "is_active": user.is_active,
    }}


@app.get("/auth/2fa-status")
def get_2fa_status(current_user: models.User = Depends(get_current_user)):
    """Returns whether 2FA is currently enabled for the authenticated user."""
    return {"is_2fa_enabled": bool(current_user.is_2fa_enabled)}


@app.post("/auth/disable-2fa")
def disable_2fa(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Disables 2FA for the authenticated user and clears the TOTP secret."""
    current_user.is_2fa_enabled = False
    current_user.totp_secret = None
    db.commit()
    return {"success": True, "message": "אימות דו-שלבי כובה"}


@app.post("/users/login", response_model=schemas.UserResponse)
def login(action: schemas.ShiftAction, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == action.passcode).first()
    if not user or not verify_passcode(action.passcode, user, db):
        raise HTTPException(status_code=401, detail="קוד שגוי")
    if user.approval_status == "pending":
        raise HTTPException(status_code=403, detail="החשבון ממתין לאישור המנהל")
    if user.approval_status == "rejected":
        raise HTTPException(status_code=403, detail="הבקשה נדחתה על ידי המנהל")
    return user


# ==========================================
# BUSINESSES
# ==========================================
def generate_join_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


@app.post("/businesses/", response_model=schemas.BusinessResponse)
def create_business(business: schemas.BusinessCreate, db: Session = Depends(get_db)):
    db_business = models.Business(name=business.name, join_code=generate_join_code())
    db.add(db_business)
    db.commit()
    db.refresh(db_business)
    return db_business


@app.get("/businesses/{business_id}/join-code")
def get_join_code(business_id: int, db: Session = Depends(get_db)):
    biz = db.query(models.Business).filter(models.Business.id == business_id).first()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found")
    if not biz.join_code:
        biz.join_code = generate_join_code()
        db.commit()
    return {"join_code": biz.join_code, "business_name": biz.name}


@app.post("/businesses/{business_id}/regenerate-code")
def regenerate_join_code(business_id: int, db: Session = Depends(get_db)):
    biz = db.query(models.Business).filter(models.Business.id == business_id).first()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found")
    biz.join_code = generate_join_code()
    db.commit()
    return {"join_code": biz.join_code}


# ==========================================
# BRANCHES
# ==========================================
@app.get("/branches/{business_id}")
def get_branches(business_id: int, db: Session = Depends(get_db)):
    branches = db.query(models.Branch).filter(models.Branch.business_id == business_id).all()
    return [{"id": b.id, "name": b.name, "address": b.address} for b in branches]


@app.post("/branches/", response_model=schemas.BranchResponse)
def create_branch(branch: schemas.BranchCreate, db: Session = Depends(get_db)):
    db_branch = models.Branch(
        business_id=branch.business_id,
        name=branch.name,
        address=branch.address or ""
    )
    db.add(db_branch)
    db.commit()
    db.refresh(db_branch)
    return db_branch


@app.put("/branches/{branch_id}")
def update_branch(branch_id: int, data: schemas.BranchCreate, db: Session = Depends(get_db)):
    branch = db.query(models.Branch).filter(models.Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.name = data.name
    branch.address = data.address or ""
    db.commit()
    return {"status": "updated"}


@app.delete("/branches/{branch_id}")
def delete_branch(branch_id: int, db: Session = Depends(get_db)):
    branch = db.query(models.Branch).filter(models.Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    db.delete(branch)
    db.commit()
    return {"status": "deleted"}


# ==========================================
# EMPLOYEES (CRUD)
# ==========================================
@app.get("/employees/{business_id}")
def get_employees(business_id: int, db: Session = Depends(get_db)):
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()
    return [{
        "id": w.id,
        "full_name": w.full_name,
        "role": w.role,
        "passcode": w.passcode,
        "hourly_rate": w.hourly_rate or 0.0,
        "phone": w.phone or "",
        "email": w.email or "",
        "branch_id": w.branch_id,
        "is_active": w.is_active if w.is_active is not None else True,
    } for w in workers]


@app.post("/employees/", response_model=schemas.UserResponse)
def create_employee(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = models.User(
        business_id=user.business_id,
        full_name=user.full_name,
        role=user.role,
        passcode=user.passcode,
        passcode_hash=hash_passcode(user.passcode),  # SECURITY: always hash before storing
        hourly_rate=user.hourly_rate,
        phone=user.phone,
        email=user.email,
        branch_id=user.branch_id,
        is_active=True,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


@app.post("/users/", response_model=schemas.UserResponse)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    return create_employee(user, db)


@app.put("/employees/{employee_id}", response_model=schemas.UserResponse)
def update_employee(employee_id: int, update: schemas.UserUpdate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == employee_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
    data = update.dict(exclude_unset=True)
    # SECURITY: if a new passcode is being set, also update the bcrypt hash
    if "passcode" in data and data["passcode"]:
        data["passcode_hash"] = hash_passcode(data["passcode"])
    for field, value in data.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@app.delete("/employees/{employee_id}")
def deactivate_employee(employee_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == employee_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
    user.is_active = False
    db.commit()
    return {"status": "deactivated"}


# ==========================================
# CLOCK IN / OUT
# ==========================================
@app.post("/shifts/clock-in", response_model=schemas.ShiftResponse)
def clock_in(action: schemas.ShiftAction, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == action.passcode).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    open_shift = db.query(models.Shift).filter(
        models.Shift.user_id == user.id, models.Shift.clock_out == None
    ).first()
    if open_shift:
        raise HTTPException(status_code=400, detail="יש לך משמרת פתוחה")
    new_shift = models.Shift(user_id=user.id, clock_in=now_israel())
    db.add(new_shift)
    db.commit()
    db.refresh(new_shift)
    return new_shift


@app.post("/shifts/clock-out", response_model=schemas.ShiftResponse)
def clock_out(action: schemas.ShiftAction, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == action.passcode).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    open_shift = db.query(models.Shift).filter(
        models.Shift.user_id == user.id, models.Shift.clock_out == None
    ).first()
    if not open_shift:
        raise HTTPException(status_code=400, detail="אין משמרת פתוחה")
    open_shift.clock_out = now_israel()
    open_shift.total_hours = round(
        (open_shift.clock_out - to_naive(open_shift.clock_in)).total_seconds() / 3600.0, 2
    )
    db.commit()
    db.refresh(open_shift)
    # Overtime alert
    if open_shift.total_hours > 9:
        create_notification(
            db, user.business_id, "overtime_warning",
            f"⚠️ {user.full_name} עבד {open_shift.total_hours} שעות - חריגת שעות!"
        )
    return open_shift


@app.get("/shifts/personal/{passcode}")
def get_personal_shifts(passcode: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == passcode).first()
    if not user:
        raise HTTPException(status_code=404)
    shifts = (
        db.query(models.Shift)
        .filter(models.Shift.user_id == user.id)
        .order_by(models.Shift.clock_in.desc())
        .limit(30)
        .all()
    )
    return shifts


@app.get("/shifts/personal-stats/{passcode}")
def get_personal_stats(passcode: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == passcode).first()
    if not user:
        raise HTTPException(status_code=404)
    now = datetime.now()
    # Fetch individual completed shifts to calculate overtime salary per-shift
    monthly_shifts = (
        db.query(models.Shift)
        .filter(
            models.Shift.user_id == user.id,
            extract("year", models.Shift.clock_in) == now.year,
            extract("month", models.Shift.clock_in) == now.month,
            models.Shift.clock_out != None,
        )
        .all()
    )
    monthly_hours = round(sum(s.total_hours for s in monthly_shifts if s.total_hours), 2)
    estimated_salary = round(sum(
        calc_shift_salary(s.total_hours, user.hourly_rate or 0.0)
        for s in monthly_shifts if s.total_hours
    ), 2)
    upcoming = (
        db.query(models.ScheduledShift)
        .filter(
            models.ScheduledShift.user_id == user.id,
            models.ScheduledShift.start_time >= datetime.now(),
        )
        .order_by(models.ScheduledShift.start_time)
        .limit(5)
        .all()
    )
    pending_leave = (
        db.query(models.LeaveRequest)
        .filter(
            models.LeaveRequest.user_id == user.id,
            models.LeaveRequest.status == "pending",
        )
        .count()
    )
    open_shift = db.query(models.Shift).filter(
        models.Shift.user_id == user.id, models.Shift.clock_out == None
    ).first()
    return {
        "user": {
            "id": user.id,
            "full_name": user.full_name,
            "hourly_rate": user.hourly_rate or 0.0,
            "business_id": user.business_id,
        },
        "monthly_hours": monthly_hours,
        "estimated_salary": estimated_salary,
        "total_shifts": db.query(models.Shift).filter(models.Shift.user_id == user.id).count(),
        "upcoming_shifts": len(upcoming),
        "pending_leave": pending_leave,
        "is_clocked_in": open_shift is not None,
        "open_shift_id": open_shift.id if open_shift else None,
    }


# ==========================================
# DASHBOARD (admin)
# ==========================================
@app.get("/dashboard/stats/{business_id}")
def get_dashboard_stats(business_id: int, db: Session = Depends(get_db)):
    users_sub = db.query(models.User.id).filter(models.User.business_id == business_id).subquery()
    active_count = db.query(models.Shift).filter(
        models.Shift.user_id.in_(users_sub), models.Shift.clock_out == None
    ).count()
    now = datetime.now()
    monthly_hours = (
        db.query(func.sum(models.Shift.total_hours))
        .filter(
            models.Shift.user_id.in_(users_sub),
            extract("year", models.Shift.clock_in) == now.year,
            extract("month", models.Shift.clock_in) == now.month,
        )
        .scalar()
        or 0.0
    )
    overtime = db.query(models.Shift).filter(
        models.Shift.user_id.in_(users_sub), models.Shift.total_hours > 9.0
    ).count()
    pending_leave = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.business_id == business_id,
        models.LeaveRequest.status == "pending",
    ).count()
    unread_notif = db.query(models.Notification).filter(
        models.Notification.business_id == business_id,
        models.Notification.is_read == False,
    ).count()
    return {
        "active_count": active_count,
        "total_hours": round(monthly_hours, 1),
        "overtime_count": overtime,
        "pending_leave": pending_leave,
        "unread_notifications": unread_notif,
    }


@app.get("/dashboard/active-workers/{business_id}")
def get_active_workers(business_id: int, db: Session = Depends(get_db)):
    users_sub = db.query(models.User.id).filter(models.User.business_id == business_id).subquery()
    open_shifts = (
        db.query(models.Shift, models.User)
        .join(models.User)
        .filter(models.Shift.user_id.in_(users_sub), models.Shift.clock_out == None)
        .all()
    )
    now = now_israel()
    return [
        {
            "shift_id": s.Shift.id,
            "full_name": s.User.full_name,
            "clock_in": s.Shift.clock_in,
            "hours_so_far": round((now - to_naive(s.Shift.clock_in)).total_seconds() / 3600.0, 1),
        }
        for s in open_shifts
    ]


@app.post("/shifts/manual-open")
def manual_open_shift(req: ManualShiftOpen, db: Session = Depends(get_db)):
    open_shift = db.query(models.Shift).filter(
        models.Shift.user_id == req.user_id,
        models.Shift.clock_out == None
    ).first()
    if open_shift:
        raise HTTPException(status_code=400, detail="יש כבר משמרת פתוחה לעובד זה")
    # מנהל יכול לפתוח משמרת בכל זמן — עבר או עתיד
    new_shift = models.Shift(user_id=req.user_id, clock_in=req.clock_in)
    db.add(new_shift)
    db.commit()
    db.refresh(new_shift)
    return new_shift


@app.post("/shifts/manual-close/{shift_id}")
def manual_close_shift(shift_id: int, req: ManualShiftClose, db: Session = Depends(get_db)):
    shift = db.query(models.Shift).filter(models.Shift.id == shift_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="משמרת לא נמצאה")
    # מנהל יכול לסגור משמרת בכל זמן — עבר או עתיד
    if req.clock_out < to_naive(shift.clock_in):
        raise HTTPException(status_code=400, detail="יציאה לפני כניסה — בדוק תאריכים")
    shift.clock_out = req.clock_out
    shift.total_hours = round((shift.clock_out - to_naive(shift.clock_in)).total_seconds() / 3600.0, 2)
    db.commit()
    db.refresh(shift)
    return shift


@app.get("/shifts/status/{business_id}")
def get_employees_shift_status(business_id: int, db: Session = Depends(get_db)):
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()
    result = []
    for w in workers:
        open_shift = db.query(models.Shift).filter(
            models.Shift.user_id == w.id,
            models.Shift.clock_out == None
        ).first()
        result.append({
            "user_id": w.id,
            "full_name": w.full_name,
            "role": w.role,
            "is_active": w.is_active,
            "clocked_in": open_shift is not None,
            "shift_id": open_shift.id if open_shift else None,
            "clock_in": open_shift.clock_in if open_shift else None,
        })
    return result


@app.post("/shifts/force-out/{shift_id}")
def force_clock_out(shift_id: int, db: Session = Depends(get_db)):
    shift = db.query(models.Shift).filter(models.Shift.id == shift_id).first()
    if shift and not shift.clock_out:
        shift.clock_out = now_israel()
        shift.total_hours = round(
            (shift.clock_out - to_naive(shift.clock_in)).total_seconds() / 3600.0, 2
        )
        db.commit()
    return {"status": "ok"}


@app.put("/shifts/edit/{shift_id}")
def edit_shift(shift_id: int, request: ShiftEditRequest, db: Session = Depends(get_db)):
    shift = db.query(models.Shift).filter(models.Shift.id == shift_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    if request.clock_out < request.clock_in:
        raise HTTPException(status_code=400, detail="זמן יציאה לא יכול להיות לפני זמן כניסה")
    shift.clock_in = request.clock_in
    shift.clock_out = request.clock_out
    shift.total_hours = round((shift.clock_out - shift.clock_in).total_seconds() / 3600.0, 2)
    db.commit()
    db.refresh(shift)
    return shift


@app.delete("/shifts/{shift_id}")
def delete_shift(shift_id: int, db: Session = Depends(get_db)):
    shift = db.query(models.Shift).filter(models.Shift.id == shift_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    db.delete(shift)
    db.commit()
    return {"ok": True}


@app.get("/dashboard/all-shifts/{business_id}")
def get_all_shifts(
    business_id: int,
    worker_id: Optional[int] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    users_sub = db.query(models.User.id).filter(models.User.business_id == business_id).subquery()
    query = db.query(models.Shift, models.User).join(models.User).filter(
        models.Shift.user_id.in_(users_sub)
    )
    if worker_id:
        query = query.filter(models.Shift.user_id == worker_id)
    if from_date:
        query = query.filter(models.Shift.clock_in >= datetime.fromisoformat(from_date))
    if to_date:
        query = query.filter(models.Shift.clock_in <= datetime.fromisoformat(to_date))
    shifts = query.order_by(models.Shift.clock_in.desc()).limit(300).all()
    return [
        {
            "shift_id": s.Shift.id,
            "user_id": s.User.id,
            "full_name": s.User.full_name,
            "clock_in": s.Shift.clock_in,
            "clock_out": s.Shift.clock_out,
            "total_hours": s.Shift.total_hours,
        }
        for s in shifts
    ]


@app.get("/dashboard/employee-summary/{business_id}")
def get_employee_summary(
    business_id: int,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()
    result = []
    for w in workers:
        query = db.query(models.Shift).filter(
            models.Shift.user_id == w.id,
            models.Shift.clock_out != None,
        )
        if from_date:
            query = query.filter(models.Shift.clock_in >= datetime.fromisoformat(from_date))
        if to_date:
            query = query.filter(models.Shift.clock_in <= datetime.fromisoformat(to_date))
        shifts = query.all()
        total_hours = sum(s.total_hours for s in shifts if s.total_hours)
        result.append({
            "user_id": w.id,
            "full_name": w.full_name,
            "total_shifts": len(shifts),
            "total_hours": round(total_hours, 2),
        })
    return result


@app.get("/dashboard/workers/{business_id}")
def get_business_workers(business_id: int, db: Session = Depends(get_db)):
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()
    return [{"id": w.id, "full_name": w.full_name} for w in workers]


# ==========================================
# SCHEDULED SHIFTS
# ==========================================
@app.get("/schedule/{business_id}")
def get_schedule(
    business_id: int,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    query = (
        db.query(models.ScheduledShift, models.User)
        .join(models.User)
        .filter(models.ScheduledShift.business_id == business_id)
    )
    if from_date:
        query = query.filter(models.ScheduledShift.start_time >= datetime.fromisoformat(from_date))
    if to_date:
        query = query.filter(models.ScheduledShift.start_time <= datetime.fromisoformat(to_date))
    if user_id:
        query = query.filter(models.ScheduledShift.user_id == user_id)
    results = query.order_by(models.ScheduledShift.start_time).all()
    return [
        {
            "id": r.ScheduledShift.id,
            "user_id": r.User.id,
            "full_name": r.User.full_name,
            "start_time": r.ScheduledShift.start_time,
            "end_time": r.ScheduledShift.end_time,
            "notes": r.ScheduledShift.notes,
        }
        for r in results
    ]


@app.post("/schedule/", response_model=schemas.ScheduledShiftResponse)
def create_scheduled_shift(shift: schemas.ScheduledShiftCreate, db: Session = Depends(get_db)):
    db_shift = models.ScheduledShift(
        user_id=shift.user_id,
        business_id=shift.business_id,
        start_time=shift.start_time,
        end_time=shift.end_time,
        notes=shift.notes or "",
    )
    db.add(db_shift)
    db.commit()
    db.refresh(db_shift)
    return db_shift


@app.delete("/schedule/{schedule_id}")
def delete_scheduled_shift(schedule_id: int, db: Session = Depends(get_db)):
    shift = db.query(models.ScheduledShift).filter(models.ScheduledShift.id == schedule_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(shift)
    db.commit()
    return {"status": "deleted"}


# ==========================================
# LEAVE REQUESTS
# ==========================================
@app.post("/leave/", response_model=schemas.LeaveRequestResponse)
def create_leave_request(request: schemas.LeaveRequestCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == request.passcode).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db_leave = models.LeaveRequest(
        user_id=user.id,
        business_id=user.business_id,
        leave_type=request.leave_type,
        start_date=request.start_date,
        end_date=request.end_date,
        reason=request.reason or "",
        status="pending",
    )
    db.add(db_leave)
    db.commit()
    db.refresh(db_leave)
    create_notification(
        db, user.business_id, "leave_pending",
        f"🏖️ {user.full_name} שלח בקשת היעדרות מ-{request.start_date} עד {request.end_date}"
    )
    return db_leave


@app.get("/leave/personal/{passcode}")
def get_personal_leave(passcode: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.passcode == passcode).first()
    if not user:
        raise HTTPException(status_code=404)
    requests = (
        db.query(models.LeaveRequest)
        .filter(models.LeaveRequest.user_id == user.id)
        .order_by(models.LeaveRequest.created_at.desc())
        .all()
    )
    return requests


@app.get("/leave/{business_id}")
def get_leave_requests(
    business_id: int,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = (
        db.query(models.LeaveRequest, models.User)
        .join(models.User)
        .filter(models.LeaveRequest.business_id == business_id)
    )
    if status:
        query = query.filter(models.LeaveRequest.status == status)
    results = query.order_by(models.LeaveRequest.created_at.desc()).all()
    return [
        {
            "id": r.LeaveRequest.id,
            "user_id": r.User.id,
            "full_name": r.User.full_name,
            "leave_type": r.LeaveRequest.leave_type,
            "start_date": r.LeaveRequest.start_date,
            "end_date": r.LeaveRequest.end_date,
            "reason": r.LeaveRequest.reason,
            "status": r.LeaveRequest.status,
            "manager_note": r.LeaveRequest.manager_note,
            "created_at": r.LeaveRequest.created_at,
        }
        for r in results
    ]


@app.put("/leave/{leave_id}/status")
def update_leave_status(leave_id: int, update: schemas.LeaveStatusUpdate, db: Session = Depends(get_db)):
    leave = db.query(models.LeaveRequest).filter(models.LeaveRequest.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
    leave.status = update.status
    leave.manager_note = update.manager_note or ""
    db.commit()
    # Notify the employee
    user = db.query(models.User).filter(models.User.id == leave.user_id).first()
    status_text = "אושרה ✅" if update.status == "approved" else "נדחתה ❌"
    create_notification(
        db, leave.business_id, f"leave_{update.status}",
        f"בקשת ההיעדרות שלך מ-{leave.start_date} עד {leave.end_date} {status_text}",
        user_id=leave.user_id,
    )
    return {"status": "updated"}


# ==========================================
# NOTIFICATIONS
# ==========================================
@app.get("/notifications/{business_id}")
def get_notifications(
    business_id: int,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    query = db.query(models.Notification).filter(
        models.Notification.business_id == business_id,
        models.Notification.is_read == False,
    )
    if user_id:
        query = query.filter(models.Notification.user_id == user_id)
    else:
        query = query.filter(models.Notification.user_id == None)
    notifications = query.order_by(models.Notification.created_at.desc()).limit(20).all()
    return notifications


@app.post("/notifications/mark-read/{notif_id}")
def mark_notification_read(notif_id: int, db: Session = Depends(get_db)):
    notif = db.query(models.Notification).filter(models.Notification.id == notif_id).first()
    if notif:
        notif.is_read = True
        db.commit()
    return {"status": "ok"}


@app.post("/notifications/mark-all-read/{business_id}")
def mark_all_read(business_id: int, db: Session = Depends(get_db)):
    db.query(models.Notification).filter(
        models.Notification.business_id == business_id,
        models.Notification.is_read == False,
        models.Notification.user_id == None,
    ).update({"is_read": True})
    db.commit()
    return {"status": "ok"}


# ==========================================
# REPORTS + EXPORT
# ==========================================
@app.get("/reports/monthly/{business_id}", response_model=schemas.MonthlyReport)
def get_monthly_report(
    business_id: int,
    year: Optional[int] = None,
    month: Optional[int] = None,
    db: Session = Depends(get_db),
):
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()
    report_rows = []
    for w in workers:
        shifts = (
            db.query(models.Shift)
            .filter(
                models.Shift.user_id == w.id,
                extract("year", models.Shift.clock_in) == year,
                extract("month", models.Shift.clock_in) == month,
                models.Shift.clock_out != None,
            )
            .all()
        )
        total_hours = sum(s.total_hours for s in shifts if s.total_hours)
        # Overtime salary: calculated per-shift so each shift triggers its own overtime tier
        estimated_salary = sum(
            calc_shift_salary(s.total_hours, w.hourly_rate or 0.0)
            for s in shifts if s.total_hours
        )
        report_rows.append(
            schemas.SalaryReportRow(
                user_id=w.id,
                full_name=w.full_name,
                role=w.role,
                total_shifts=len(shifts),
                total_hours=round(total_hours, 2),
                hourly_rate=w.hourly_rate or 0.0,
                estimated_salary=round(estimated_salary, 2),
            )
        )
    return schemas.MonthlyReport(
        year=year,
        month=month,
        employees=report_rows,
        grand_total_hours=round(sum(r.total_hours for r in report_rows), 2),
        grand_total_salary=round(sum(r.estimated_salary for r in report_rows), 2),
    )


@app.get("/reports/export/{business_id}")
def export_monthly_csv(
    business_id: int,
    year: Optional[int] = None,
    month: Optional[int] = None,
    db: Session = Depends(get_db),
):
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["שם עובד", "תפקיד", "משמרות", 'סה"כ שעות', "תעריף שעתי (₪)", "שכר משוער (₪)"])

    for w in workers:
        shifts = (
            db.query(models.Shift)
            .filter(
                models.Shift.user_id == w.id,
                extract("year", models.Shift.clock_in) == year,
                extract("month", models.Shift.clock_in) == month,
                models.Shift.clock_out != None,
            )
            .all()
        )
        total_hours = sum(s.total_hours for s in shifts if s.total_hours)
        estimated_salary = sum(
            calc_shift_salary(s.total_hours, w.hourly_rate or 0.0)
            for s in shifts if s.total_hours
        )
        writer.writerow([
            w.full_name,
            w.role,
            len(shifts),
            round(total_hours, 2),
            w.hourly_rate or 0.0,
            round(estimated_salary, 2),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=report_{year}_{month:02d}.csv"},
    )


# ==========================================
# AI ASSISTANT
# ==========================================
@app.post("/ai/manager-ask")
def manager_ai_ask(payload: dict, db: Session = Depends(get_db)):
    business_id = payload.get("business_id")
    query = payload.get("query")
    history = payload.get("history", [])  # היסטוריית שיחה מהצד הלקוח

    users_sub = db.query(models.User.id).filter(models.User.business_id == business_id).subquery()

    # נתוני עובדים
    workers = db.query(models.User).filter(models.User.business_id == business_id).all()
    workers_context = "\n".join([
        f"- {w.full_name} (תפקיד: {w.role}, תעריף: {w.hourly_rate or 0}₪/שעה)"
        for w in workers
    ])

    # משמרות אחרונות
    shifts = (
        db.query(models.Shift, models.User)
        .join(models.User)
        .filter(models.Shift.user_id.in_(users_sub))
        .order_by(models.Shift.clock_in.desc())
        .limit(50)
        .all()
    )
    shifts_context = "\n".join([
        f"- {s.User.full_name}: {s.Shift.clock_in.strftime('%d/%m/%Y %H:%M')} | {s.Shift.total_hours or 'פתוח'} שעות"
        for s in shifts
    ])

    system_prompt = (
        f"אתה עוזר AI חכם של מנהל עסק. ענה בעברית בצורה ברורה וממוקדת.\n\n"
        f"**עובדים בעסק:**\n{workers_context}\n\n"
        f"**50 המשמרות האחרונות:**\n{shifts_context}"
    )

    # בנה היסטוריית שיחה לפורמט של Groq (OpenAI-compatible)
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            messages.append({"role": "user", "content": content})
        elif role == "model":
            messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": query})

    try:
        if not _groq_client:
            return {"answer": "שגיאת AI: GROQ_API_KEY לא מוגדר בסביבה."}
        response = _groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=1024,
        )
        return {"answer": response.choices[0].message.content}
    except Exception as e:
        return {"answer": f"שגיאת AI: {str(e)}"}
