"""
החלפות משמרות ("מי לוקח?").

זרימה:
1. עובד מבקש מחליף למשמרת שלו (או המנהל מפרסם משמרת פנויה).
2. כל שאר העובדים מקבלים התראה ורואים את המשמרת בפורטל, ויכולים לסמן "אני לוקח/ת".
3. המנהל רואה את המתנדבים (כולל אזהרות של שומר החוק) ומאשר אחד.
4. רק אחרי האישור הסידור מתעדכן, וכולם מקבלים הודעה: המחליף, המבקש, ושאר המתנדבים.
"""
from datetime import date, datetime

from sqlalchemy.orm import Session

import agents
import models


class SwapError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _label(st: str) -> str:
    return agents.SHIFT_NAMES.get(st, st)


def _when(d: date, st: str) -> str:
    s, e = agents.SHIFT_WINDOWS[st]
    return f"{_label(st)} ביום {agents.DAY_NAMES[agents.day_index(d)]} {d:%d/%m} ({s:%H:%M}–{e:%H:%M})"


def _roster_row(db: Session, business_id: int, user_id: int, d: date):
    return db.query(models.ShiftRoster).filter(
        models.ShiftRoster.business_id == business_id,
        models.ShiftRoster.user_id == user_id,
        models.ShiftRoster.week_start == agents.week_start_of(d),
        models.ShiftRoster.day_of_week == agents.day_index(d),
    ).first()


def _workers(db: Session, business_id: int):
    return [u for u in db.query(models.User).filter(models.User.business_id == business_id).all()
            if agents._is_schedulable(u)]


def _notify(db: Session, business_id: int, user_id, type_: str, message: str):
    db.add(models.Notification(business_id=business_id, user_id=user_id, type=type_, message=message, is_read=False))


def _get_swap(db: Session, business_id: int, swap_id: int) -> models.ShiftSwap:
    s = db.query(models.ShiftSwap).filter(models.ShiftSwap.id == swap_id,
                                          models.ShiftSwap.business_id == business_id).first()
    if not s:
        raise SwapError("הבקשה לא נמצאה", 404)
    return s


def _broadcast(db: Session, swap: models.ShiftSwap, requester_name: str | None, exclude_ids: set):
    who = f"{requester_name} מחפש/ת מחליף ל" if requester_name else "משמרת פנויה: "
    msg = f"🔄 {who}{_when(swap.date, swap.shift_type)}. אפשר לקחת אותה בפורטל האישי"
    for u in _workers(db, swap.business_id):
        if u.id not in exclude_ids:
            _notify(db, swap.business_id, u.id, "open_shift", msg)


# ---------------------------------------------------------------------------
# עובד
# ---------------------------------------------------------------------------
def request_swap(db: Session, user: models.User, d: date, note: str, now: datetime) -> models.ShiftSwap:
    if d < now.date():
        raise SwapError("אי אפשר לבקש מחליף למשמרת שכבר עברה")
    row = _roster_row(db, user.business_id, user.id, d)
    if not row or row.shift_type not in agents.SHIFT_WINDOWS:
        raise SwapError("אין לך משמרת ביום הזה")
    dup = db.query(models.ShiftSwap).filter(models.ShiftSwap.business_id == user.business_id,
                                            models.ShiftSwap.requester_id == user.id,
                                            models.ShiftSwap.date == d,
                                            models.ShiftSwap.status == "open").first()
    if dup:
        raise SwapError("כבר ביקשת מחליף למשמרת הזו")
    swap = models.ShiftSwap(business_id=user.business_id, requester_id=user.id, date=d, shift_type=row.shift_type,
                            note=(note or "").strip()[:200] or None, status="open", created_at=now)
    db.add(swap)
    db.flush()
    _broadcast(db, swap, user.full_name, {user.id})
    _notify(db, user.business_id, None, "swap_request",
            f"🔄 {user.full_name} ביקש/ה מחליף ל{_when(d, row.shift_type)}. העובדים קיבלו הודעה")
    agents.log_audit(db, user.business_id, user, "swap.request", f"{user.full_name} ביקש/ה מחליף ל{_when(d, row.shift_type)}",
                     "swap", swap.id, now=now)
    return swap


def open_swaps_for_worker(db: Session, user: models.User, now: datetime) -> dict:
    today = now.date()
    swaps = db.query(models.ShiftSwap).filter(models.ShiftSwap.business_id == user.business_id,
                                              models.ShiftSwap.date >= today).order_by(models.ShiftSwap.date).all()
    names = {u.id: u.full_name for u in db.query(models.User).filter(models.User.business_id == user.business_id).all()}
    vols = {}
    for v in db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id.in_([s.id for s in swaps] or [0])).all():
        vols.setdefault(v.swap_id, []).append(v)
    available, mine = [], []
    for s in swaps:
        s0, s1 = agents.SHIFT_WINDOWS[s.shift_type]
        base = {"id": s.id, "date": s.date.isoformat(), "day": agents.DAY_NAMES[agents.day_index(s.date)],
                "shift_type": s.shift_type, "label": _label(s.shift_type), "start": f"{s0:%H:%M}", "end": f"{s1:%H:%M}",
                "note": s.note or "", "status": s.status}
        if s.requester_id == user.id:
            mine.append({**base, "volunteers": len([v for v in vols.get(s.id, []) if v.status == "pending"]),
                         "filled_by": names.get(s.filled_by) if s.filled_by else None})
            continue
        my_vol = next((v for v in vols.get(s.id, []) if v.user_id == user.id), None)
        if s.status != "open" and not (my_vol and my_vol.status == "approved"):
            continue
        reason = None
        if s.status == "open" and _roster_row(db, user.business_id, user.id, s.date):
            reason = "כבר יש לך משמרת ביום הזה"
        available.append({**base, "requester": names.get(s.requester_id) if s.requester_id else None,
                          "my_status": my_vol.status if my_vol else None,
                          "can_volunteer": s.status == "open" and reason is None and not my_vol,
                          "reason": reason})
    return {"available": available, "mine": mine}


def volunteer(db: Session, user: models.User, swap_id: int, now: datetime) -> models.SwapVolunteer:
    swap = _get_swap(db, user.business_id, swap_id)
    if swap.status != "open":
        raise SwapError("המשמרת כבר לא פנויה")
    if swap.requester_id == user.id:
        raise SwapError("זו הבקשה שלך")
    if swap.date < now.date():
        raise SwapError("המשמרת כבר עברה")
    if not agents._is_schedulable(user):
        raise SwapError("רק עובד פעיל יכול לקחת משמרת", 403)
    if _roster_row(db, user.business_id, user.id, swap.date):
        raise SwapError("כבר יש לך משמרת ביום הזה")
    if db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id == swap.id,
                                             models.SwapVolunteer.user_id == user.id).first():
        raise SwapError("כבר סימנת שאת/ה לוקח/ת את המשמרת")
    v = models.SwapVolunteer(swap_id=swap.id, user_id=user.id, status="pending", created_at=now)
    db.add(v)
    _notify(db, user.business_id, None, "swap_volunteer",
            f"✋ {user.full_name} מוכן/ה לקחת {_when(swap.date, swap.shift_type)}. מחכה לאישורך")
    return v


def withdraw(db: Session, user: models.User, swap_id: int):
    swap = _get_swap(db, user.business_id, swap_id)
    v = db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id == swap.id,
                                              models.SwapVolunteer.user_id == user.id,
                                              models.SwapVolunteer.status == "pending").first()
    if not v:
        raise SwapError("לא נמצאה התנדבות פעילה")
    db.delete(v)


def cancel(db: Session, actor: models.User, swap_id: int, now: datetime):
    swap = _get_swap(db, actor.business_id, swap_id)
    if actor.role != "manager" and swap.requester_id != actor.id:
        raise SwapError("אפשר לבטל רק בקשה שלך", 403)
    if swap.status != "open":
        raise SwapError("הבקשה כבר טופלה")
    swap.status, swap.resolved_at = "cancelled", now
    for v in db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id == swap.id,
                                                   models.SwapVolunteer.status == "pending").all():
        v.status = "declined"
        _notify(db, swap.business_id, v.user_id, "swap_cancelled",
                f"הבקשה ל{_when(swap.date, swap.shift_type)} בוטלה. תודה שהצעת לעזור!")
    agents.log_audit(db, actor.business_id, actor, "swap.cancel", f"ביטול בקשת החלפה ל{_when(swap.date, swap.shift_type)}",
                     "swap", swap.id, now=now)


# ---------------------------------------------------------------------------
# מנהל
# ---------------------------------------------------------------------------
def post_open_shift(db: Session, manager: models.User, d: date, shift_type: str, note: str,
                    replaces_employee_id: int | None, now: datetime) -> models.ShiftSwap:
    if shift_type not in agents.SHIFT_WINDOWS:
        raise SwapError("סוג משמרת לא תקין")
    if d < now.date():
        raise SwapError("אי אפשר לפרסם משמרת שכבר עברה")
    requester = None
    if replaces_employee_id:
        requester = db.query(models.User).filter(models.User.id == replaces_employee_id,
                                                 models.User.business_id == manager.business_id).first()
        if not requester:
            raise SwapError("העובד לא נמצא", 404)
    swap = models.ShiftSwap(business_id=manager.business_id, requester_id=requester.id if requester else None,
                            date=d, shift_type=shift_type, note=(note or "").strip()[:200] or None,
                            status="open", created_at=now)
    db.add(swap)
    db.flush()
    _broadcast(db, swap, requester.full_name if requester else None, {requester.id} if requester else set())
    agents.log_audit(db, manager.business_id, manager, "swap.post",
                     f"פרסום משמרת פנויה לכל העובדים: {_when(d, shift_type)}", "swap", swap.id, now=now)
    return swap


def list_for_manager(db: Session, business_id: int, now: datetime, include_closed: bool = False) -> list[dict]:
    q = db.query(models.ShiftSwap).filter(models.ShiftSwap.business_id == business_id)
    if not include_closed:
        q = q.filter(models.ShiftSwap.status == "open", models.ShiftSwap.date >= now.date())
    swaps = q.order_by(models.ShiftSwap.date, models.ShiftSwap.id).limit(100).all()
    users = {u.id: u for u in db.query(models.User).filter(models.User.business_id == business_id).all()}
    out = []
    for s in swaps:
        vols = db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id == s.id)\
            .order_by(models.SwapVolunteer.created_at).all()
        vlist = []
        for v in vols:
            u = users.get(v.user_id)
            if not u:
                continue
            warnings = []
            if s.status == "open" and v.status == "pending":
                warnings = [i["message"] for i in agents.check_hypothetical(db, business_id, u.id, s.date, s.shift_type)
                            if i["severity"] != "info"]
            vlist.append({"user_id": u.id, "name": u.full_name, "status": v.status, "warnings": warnings,
                          "created_at": v.created_at.isoformat(timespec="minutes") if v.created_at else None})
        s0, s1 = agents.SHIFT_WINDOWS[s.shift_type]
        out.append({"id": s.id, "date": s.date.isoformat(), "day": agents.DAY_NAMES[agents.day_index(s.date)],
                    "shift_type": s.shift_type, "label": _label(s.shift_type), "start": f"{s0:%H:%M}", "end": f"{s1:%H:%M}",
                    "requester_id": s.requester_id,
                    "requester": users[s.requester_id].full_name if s.requester_id in users else None,
                    "note": s.note or "", "status": s.status,
                    "filled_by": users[s.filled_by].full_name if s.filled_by in users else None,
                    "volunteers": vlist})
    return out


def approve(db: Session, manager: models.User, swap_id: int, user_id: int, now: datetime) -> dict:
    # row lock: two managers approving at once can't both fill the same shift
    db.query(models.ShiftSwap.id).filter(models.ShiftSwap.id == swap_id).with_for_update().first()
    swap = _get_swap(db, manager.business_id, swap_id)
    if swap.status != "open":
        raise SwapError("הבקשה כבר טופלה")
    vol = db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id == swap.id,
                                                models.SwapVolunteer.user_id == user_id,
                                                models.SwapVolunteer.status == "pending").first()
    if not vol:
        raise SwapError("העובד לא התנדב למשמרת הזו")
    taker = db.query(models.User).filter(models.User.id == user_id, models.User.business_id == manager.business_id).first()
    if not taker or not agents._is_schedulable(taker):
        raise SwapError("העובד לא פעיל")
    if _roster_row(db, manager.business_id, taker.id, swap.date):
        raise SwapError(f"ל{taker.full_name} כבר יש משמרת ביום הזה")
    warnings = [i["message"] for i in agents.check_hypothetical(db, manager.business_id, taker.id, swap.date, swap.shift_type)
                if i["severity"] != "info"]
    week, didx = agents.week_start_of(swap.date), agents.day_index(swap.date)
    position = None
    if swap.requester_id:
        old = _roster_row(db, manager.business_id, swap.requester_id, swap.date)
        if old:
            position = old.position
            db.delete(old)
    db.add(models.ShiftRoster(business_id=manager.business_id, user_id=taker.id, week_start=week,
                              day_of_week=didx, shift_type=swap.shift_type, position=position))
    swap.status, swap.filled_by, swap.resolved_at = "filled", taker.id, now
    vol.status = "approved"
    when = _when(swap.date, swap.shift_type)
    _notify(db, manager.business_id, taker.id, "swap_approved", f"✅ המנהל אישר: {when} שלך. תודה!")
    requester = db.get(models.User, swap.requester_id) if swap.requester_id else None
    if requester:
        _notify(db, manager.business_id, requester.id, "swap_filled",
                f"✅ נמצא לך מחליף: {taker.full_name} לוקח/ת את ה{when}. את/ה משוחרר/ת מהמשמרת")
    for other in db.query(models.SwapVolunteer).filter(models.SwapVolunteer.swap_id == swap.id,
                                                       models.SwapVolunteer.status == "pending",
                                                       models.SwapVolunteer.id != vol.id).all():
        other.status = "declined"
        _notify(db, manager.business_id, other.user_id, "swap_taken",
                f"ה{when} כבר נלקחה על ידי עובד אחר. תודה שהצעת לעזור!")
    agents.log_audit(db, manager.business_id, manager, "swap.approve",
                     f"אישור החלפה: {taker.full_name} לוקח/ת {when}" + (f" במקום {requester.full_name}" if requester else ""),
                     "swap", swap.id, after={"taker": taker.id, "requester": swap.requester_id}, now=now)
    return {"ok": True, "taker": taker.full_name, "warnings": warnings}


def pending_count(db: Session, business_id: int, today: date) -> tuple[int, int]:
    """(בקשות פתוחות, בקשות פתוחות שיש להן מתנדב שמחכה לאישור)"""
    swaps = db.query(models.ShiftSwap).filter(models.ShiftSwap.business_id == business_id,
                                              models.ShiftSwap.status == "open",
                                              models.ShiftSwap.date >= today).all()
    ids = [s.id for s in swaps]
    with_vol = {v.swap_id for v in db.query(models.SwapVolunteer).filter(
        models.SwapVolunteer.swap_id.in_(ids or [0]), models.SwapVolunteer.status == "pending").all()}
    return len(swaps), len(with_vol)
