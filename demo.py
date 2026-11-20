"""
מצב הדגמה (Demo) — עסק לדוגמה עם נתונים מלאים, לקישור בקורות חיים.

- העסק מזוהה לפי קוד הצטרפות שמור (DEMO_JOIN_CODE). הוא נפרד לגמרי מהעסקים האמיתיים:
  כל ההרשאות בשרת בודקות business_id, כך שמשתמש דמו לא יכול לגשת לנתונים אחרים.
- כניסה בלי סיסמה ובלי 2FA דרך POST /demo/login (טוקן עם demo=True).
- הנתונים נבנים מחדש אם עברו יותר מ-RESET_HOURS שעות מהבנייה האחרונה (או אחרי הפעלה מחדש של השרת),
  וכל התאריכים יחסיים להיום — כך שההדגמה תמיד נראית עדכנית.
"""
import random
import secrets
import threading
from datetime import datetime, timedelta, date, time

import models

DEMO_JOIN_CODE = "DEMO00"
DEMO_BUSINESS_NAME = "קפה הנמל"
DEMO_MANAGER_NAME = "מיכל אברהם"
DEMO_EMPLOYEE_NAME = "נועה לוי"
RESET_HOURS = 6

# name, hourly rate, branch index, default position, usual shift
WORKERS = [
    ("נועה לוי", 45, 0, "קופה", "morning"),
    ("דניאל כהן", 42, 0, "בר", "evening"),
    ("יוסי מזרחי", 40, 1, "מטבח", "morning"),
    ("שירה פרץ", 38, 0, "מלצרות", "evening"),
    ("עומר ביטון", 50, 0, "מטבח", "morning"),
    ("מאיה גולן", 36, 1, "מלצרות", "evening"),
    ("איתן שפירא", 40, 1, "בר", "evening"),
    ("רוני אזולאי", 37, 0, "מלצרות", "morning"),
    ("תמר רוזן", 44, 1, "קופה", "morning"),
]

_lock = threading.Lock()
_last_reset: datetime | None = None


def is_demo_business(db, business_id: int) -> bool:
    biz = db.query(models.Business).filter(models.Business.id == business_id).first()
    return bool(biz and biz.join_code == DEMO_JOIN_CODE)


def ensure_demo(db, hash_passcode, now: datetime) -> models.Business:
    """מחזיר את עסק הדמו, ובונה אותו מחדש אם צריך."""
    global _last_reset
    with _lock:
        biz = db.query(models.Business).filter(models.Business.join_code == DEMO_JOIN_CODE).first()
        stale = _last_reset is None or (now - _last_reset) > timedelta(hours=RESET_HOURS)
        if biz is None or stale:
            biz = _rebuild(db, biz, hash_passcode, now)
            _last_reset = now
        return biz


def _wipe(db, biz):
    bid = biz.id
    user_ids = [u.id for u in db.query(models.User.id).filter(models.User.business_id == bid).all()]
    for model in (models.Notification, models.LeaveRequest, models.ShiftPreference,
                  models.ShiftRoster, models.ScheduledShift):
        db.query(model).filter(model.business_id == bid).delete(synchronize_session=False)
    if user_ids:
        db.query(models.Notification).filter(models.Notification.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(models.Shift).filter(models.Shift.user_id.in_(user_ids)).delete(synchronize_session=False)
    db.query(models.User).filter(models.User.business_id == bid).delete(synchronize_session=False)
    db.query(models.Branch).filter(models.Branch.business_id == bid).delete(synchronize_session=False)
    db.flush()


def _week_start(d: date) -> date:
    # השבוע מתחיל ביום ראשון. Python: Monday=0 ... Sunday=6
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _rebuild(db, biz, hash_passcode, now: datetime) -> models.Business:
    rng = random.Random(2026)
    if biz is None:
        biz = models.Business(name=DEMO_BUSINESS_NAME, join_code=DEMO_JOIN_CODE)
        db.add(biz)
        db.flush()
    else:
        biz.name = DEMO_BUSINESS_NAME
        _wipe(db, biz)

    # nobody can log in to demo users with a password — access is only via /demo/login
    def random_hash():
        return hash_passcode(secrets.token_urlsafe(24))

    branches = [
        models.Branch(business_id=biz.id, name="נמל תל אביב", address="נמל תל אביב, האנגר 12"),
        models.Branch(business_id=biz.id, name="שרונה", address="שרונה מרקט, תל אביב"),
    ]
    db.add_all(branches)
    db.flush()

    manager = models.User(
        business_id=biz.id, full_name=DEMO_MANAGER_NAME, role="manager",
        passcode=None, passcode_hash=random_hash(), hourly_rate=0, phone="050-1234567",
        email="michal@cafe-hanamal.example", is_active=True, approval_status="approved",
        is_2fa_enabled=1, branch_id=branches[0].id,
    )
    db.add(manager)

    workers = []
    for i, (name, rate, br, pos, usual) in enumerate(WORKERS):
        u = models.User(
            business_id=biz.id, full_name=name, role="worker", passcode=None,
            passcode_hash=random_hash(), hourly_rate=rate, phone=f"05{rng.randint(0, 8)}-{rng.randint(1000000, 9999999)}",
            is_active=True, approval_status="approved", branch_id=branches[br].id,
        )
        u._pos, u._usual = pos, usual
        workers.append(u)
    db.add_all(workers)
    # a pending sign-up waiting for the manager's approval
    pending = models.User(
        business_id=biz.id, full_name="אביב קליין", role="worker", passcode=None,
        passcode_hash=random_hash(), phone="054-7788990", is_active=False, approval_status="pending",
    )
    db.add(pending)
    db.flush()

    today = now.date()

    def at(d: date, hh: int, mm: int) -> datetime:
        return datetime.combine(d, time(hh, mm))

    # ---- past shifts: last 35 days ----
    shifts = []
    for back in range(35, 0, -1):
        d = today - timedelta(days=back)
        for w in workers:
            if rng.random() > 0.62:
                continue
            kind = w._usual if rng.random() < 0.8 else ("evening" if w._usual == "morning" else "morning")
            if rng.random() < 0.05:
                kind = "double"
            if kind == "morning":
                cin = at(d, 10, rng.randint(15, 40))
                cout = at(d, 17, rng.randint(50, 59)) + timedelta(minutes=rng.randint(0, 25))
            elif kind == "evening":
                cin = at(d, 17, rng.randint(45, 59)) + timedelta(minutes=rng.randint(0, 10))
                cout = at(d, 23, rng.randint(0, 40))
            else:
                cin = at(d, 10, rng.randint(20, 35))
                cout = at(d, 21, rng.randint(15, 59))
            shifts.append(models.Shift(user_id=w.id, clock_in=cin, clock_out=cout,
                                       total_hours=round((cout - cin).total_seconds() / 3600, 2)))
    # ---- today: a few people on shift right now ----
    for w, hours_ago in ((workers[0], 3.2), (workers[4], 2.6), (workers[7], 1.1)):
        shifts.append(models.Shift(user_id=w.id, clock_in=now - timedelta(hours=hours_ago)))
    db.add_all(shifts)

    # ---- weekly roster: this week fully, next week partially (so preferences make sense) ----
    this_week = _week_start(today)
    next_week = this_week + timedelta(days=7)
    roster = []
    for week, days in ((this_week, range(7)), (next_week, range(3))):
        for day in days:
            for w in rng.sample(workers, 6):
                kind = w._usual if rng.random() < 0.85 else "double"
                roster.append(models.ShiftRoster(
                    business_id=biz.id, user_id=w.id, week_start=week, day_of_week=day,
                    shift_type=kind, position=w._pos,
                    note="סגירת קופה" if (w._pos == "קופה" and kind != "morning") else None,
                ))
    db.add_all(roster)

    prefs = []
    for w in workers:
        for day in rng.sample(range(3, 7), 2):
            prefs.append(models.ShiftPreference(business_id=biz.id, user_id=w.id, week_start=next_week,
                                                day_of_week=day, preference=w._usual))
    db.add_all(prefs)

    # ---- upcoming scheduled shifts (feeds "upcoming shifts" in the employee portal) ----
    times = {"morning": ((10, 30), (18, 0)), "evening": ((18, 0), (23, 0)), "double": ((10, 30), (23, 0))}
    sched = []
    for r in roster:
        d = r.week_start + timedelta(days=r.day_of_week)
        if today < d <= today + timedelta(days=7):
            (sh, sm), (eh, em) = times[r.shift_type]
            sched.append(models.ScheduledShift(user_id=r.user_id, business_id=biz.id,
                                               start_time=at(d, sh, sm), end_time=at(d, eh, em),
                                               notes=r.position or ""))
    db.add_all(sched)

    # ---- leave requests ----
    noa, daniel, yossi, shira = workers[0], workers[1], workers[2], workers[3]
    db.add_all([
        models.LeaveRequest(user_id=noa.id, business_id=biz.id, leave_type="vacation",
                            start_date=today + timedelta(days=10), end_date=today + timedelta(days=12),
                            reason="חתונה של חברה באילת", status="pending"),
        models.LeaveRequest(user_id=yossi.id, business_id=biz.id, leave_type="personal",
                            start_date=today + timedelta(days=4), end_date=today + timedelta(days=4),
                            reason="מבחן בטכניון", status="pending"),
        models.LeaveRequest(user_id=shira.id, business_id=biz.id, leave_type="vacation",
                            start_date=today - timedelta(days=14), end_date=today - timedelta(days=11),
                            reason="טיול משפחתי", status="approved", manager_note="תהני!"),
        models.LeaveRequest(user_id=daniel.id, business_id=biz.id, leave_type="vacation",
                            start_date=today + timedelta(days=2), end_date=today + timedelta(days=6),
                            reason="", status="rejected", manager_note="שבוע עמוס, בוא נמצא תאריכים אחרים"),
        models.LeaveRequest(user_id=noa.id, business_id=biz.id, leave_type="sick",
                            start_date=today - timedelta(days=20), end_date=today - timedelta(days=19),
                            reason="", status="approved"),
    ])

    # ---- notifications ----
    overtime = next((s for s in shifts if s.total_hours and s.total_hours > 9), None)
    notes = [
        models.Notification(business_id=biz.id, type="registration_pending",
                            message="🆕 אביב קליין מבקש להצטרף לעסק - ממתין לאישורך", is_read=False),
        models.Notification(business_id=biz.id, type="leave_pending",
                            message=f"🏖️ נועה לוי שלחה בקשת היעדרות מ-{today + timedelta(days=10)} עד {today + timedelta(days=12)}",
                            is_read=False),
        models.Notification(business_id=biz.id, type="leave_pending",
                            message=f"🏖️ יוסי מזרחי שלח בקשת היעדרות ל-{today + timedelta(days=4)}", is_read=False),
        models.Notification(business_id=biz.id, user_id=noa.id, type="leave_approved",
                            message="בקשת ההיעדרות שלך אושרה ✅", is_read=False),
    ]
    if overtime:
        who = next(w for w in workers if w.id == overtime.user_id)
        notes.append(models.Notification(business_id=biz.id, type="overtime_warning",
                                         message=f"⚠️ {who.full_name} עבד {overtime.total_hours} שעות - חריגת שעות!",
                                         is_read=False))
    db.add_all(notes)
    db.commit()
    db.refresh(biz)
    return biz


def demo_user(db, biz, role: str) -> models.User:
    name = DEMO_MANAGER_NAME if role == "manager" else DEMO_EMPLOYEE_NAME
    return db.query(models.User).filter(models.User.business_id == biz.id,
                                        models.User.full_name == name).first()
