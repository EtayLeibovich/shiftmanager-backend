"""
סוכני המערכת — מנועים דטרמיניסטיים (בלי LLM). הממשק קורא להם ישירות, וסוכן ה-AI
משתמש בהם ככלים ורק מנסח את התוצאה. כך המספרים תמיד נכונים, מהירים ובחינם.

1. שומר החוק בסידור   — check_week()            : מנוחה שבועית, נוער, שעות ביום/בשבוע, חופשות
2. תדריך בוקר          — daily_briefing()        : מי לא הגיע, מי שכח להחתים, מה ממתין לך היום
3. מוצא מחליף          — suggest_replacements()  : מי הכי מתאים להחליף, בלי לעבור על החוק
4. מבקר טרום-שכר      — payroll_audit()         : טעויות נוכחות ושכר לפני שהתלוש יוצא
5. יומן שינויים         — log_audit()/verify_chain(): מי שינה מה ומתי, בשרשרת hash שקשה לזייף

הבדיקות הן כלי עזר בלבד ואינן ייעוץ משפטי.
"""
import hashlib
import hmac
import json
import os
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

import models

DAY_NAMES = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
SHIFT_NAMES = {"morning": "בוקר", "evening": "ערב", "double": "כפולה", "off": "חופש"}
# Must match SHIFT_TIMES in main.py (the hours shown to employees).
SHIFT_WINDOWS = {
    "morning": (time(10, 30), time(18, 0)),
    "evening": (time(18, 0), time(23, 0)),
    "double": (time(10, 30), time(23, 0)),
}

# ---- כללים (חוק שעות עבודה ומנוחה, חוק עבודת הנוער, חוק שכר מינימום) ----
ADULT_MAX_DAY_HOURS = 12.0      # יום עבודה כולל שעות נוספות
REGULAR_WEEK_HOURS = 42.0       # שבוע עבודה רגיל
MAX_WEEK_HOURS = 58.0           # 42 + עד 16 שעות נוספות בשבוע
WEEKLY_REST_HOURS = 36.0        # מנוחה שבועית רצופה
REST_MIN_OVERLAP_HOURS = 12.0   # כמה מהמנוחה צריך ליפול בתוך השבוע עצמו
YOUTH_MAX_DAY_HOURS = 8.0
YOUTH_MAX_EVE_OF_REST_HOURS = 7.0   # נוער: ביום שלפני המנוחה השבועית (שישי)
YOUTH_MAX_WEEK_HOURS = 40.0
YOUTH_MIN_AGE = 15                  # 14–15: רק בחופשות לימודים ובעבודה קלה; מתחת ל-14 אסור
LATE_GRACE = timedelta(minutes=15)
FORGOT_CLOCK_OUT_HOURS = 14.0
LONG_OPEN_SHIFT_HOURS = 10.0
MAX_ACTUAL_SHIFT_HOURS = 12.0
# שכר מינימום לשעה (בסיס 182 שעות), מתעדכן כל 1 באפריל
MIN_WAGE_TABLE = [(date(2025, 4, 1), 34.32), (date(2026, 4, 1), 35.40)]
# נוער, מ-1.4.2026 (חודשי / 173): מתחת ל-16 = 70%, בן 16 = 75%, בן 17 = 83%
YOUTH_MIN_WAGE = {14: 26.07, 15: 26.07, 16: 27.94, 17: 30.92}
# צו ההרחבה בהסעדה (40 ₪ מ-1.4.2026) חל על קבלני הסעדה מוסדית — לא על מסעדות. כבוי כברירת מחדל.
_cat = os.getenv("CATERING_MIN_WAGE_HOURLY")
CATERING_MIN_WAGE_HOURLY = float(_cat) if _cat else None
# זמני שקיעה משוערים בתל אביב לפי חודש (שעון ישראל, כולל שעון קיץ) — לחישוב שבת בקירוב
SUNSET_BY_MONTH = {1: time(16, 55), 2: time(17, 20), 3: time(17, 45), 4: time(19, 5), 5: time(19, 25), 6: time(19, 45),
                   7: time(19, 45), 8: time(19, 20), 9: time(18, 40), 10: time(17, 55), 11: time(16, 40), 12: time(16, 40)}


def min_wage_on(d: date) -> float:
    rate = MIN_WAGE_TABLE[0][1]
    for since, value in MIN_WAGE_TABLE:
        if d >= since:
            rate = value
    return rate


def shabbat_window(friday: date) -> tuple[datetime, datetime]:
    """שבת בקירוב: משקיעה ביום שישי עד 40 דקות אחרי השקיעה במוצאי שבת."""
    saturday = friday + timedelta(days=1)
    return (datetime.combine(friday, SUNSET_BY_MONTH[friday.month]),
            datetime.combine(saturday, SUNSET_BY_MONTH[saturday.month]) + timedelta(minutes=40))

DISCLAIMER = "בדיקה אוטומטית לעזרה בלבד, לא ייעוץ משפטי. הכללים המלאים: kolzchut.org.il"
SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


# ---------------------------------------------------------------------------
# עזרים
# ---------------------------------------------------------------------------
def week_start_of(d: date) -> date:
    """השבוע מתחיל ביום ראשון."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def day_index(d: date) -> int:
    return (d.weekday() + 1) % 7


def shift_interval(d: date, shift_type: str) -> tuple[datetime, datetime]:
    start, end = SHIFT_WINDOWS[shift_type]
    return datetime.combine(d, start), datetime.combine(d, end)


def shift_hours(shift_type: str) -> float:
    s, e = shift_interval(date(2000, 1, 2), shift_type)
    return (e - s).total_seconds() / 3600


def age_on(birth: date | None, d: date) -> int | None:
    if not birth:
        return None
    return d.year - birth.year - ((d.month, d.day) < (birth.month, birth.day))


def naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _fmt_h(h: float) -> str:
    return f"{h:.1f}".rstrip("0").rstrip(".")


def _ddmm(d: date) -> str:
    return d.strftime("%d/%m")


def _issue(severity: str, code: str, message: str, user=None, d: date | None = None, **extra) -> dict:
    return {
        "severity": severity, "code": code, "message": message,
        "employee_id": user.id if user is not None else None,
        "name": user.full_name if user is not None else None,
        "date": d.isoformat() if d else None,
        "day": DAY_NAMES[day_index(d)] if d else None,
        **extra,
    }


def _sort_issues(issues: list[dict]) -> list[dict]:
    return sorted(issues, key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), i.get("date") or "", i.get("name") or ""))


def _summary(issues: list[dict]) -> dict:
    out = {"high": 0, "medium": 0, "info": 0}
    for i in issues:
        out[i["severity"]] = out.get(i["severity"], 0) + 1
    return out


def _is_schedulable(u: models.User) -> bool:
    return u.role != "manager" and u.is_active is not False and (u.approval_status or "approved") == "approved"


# ---------------------------------------------------------------------------
# 1. שומר החוק בסידור
# ---------------------------------------------------------------------------
class RosterContext:
    """כל מה שצריך כדי לבדוק שבוע אחד: עובדים, סידור (השבוע + השבועות הצמודים), חופשות."""

    def __init__(self, db: Session, business_id: int, week: date, week_entries: list | None = None):
        self.week = week_start_of(week)
        self.week_end = self.week + timedelta(days=7)
        self.users = {u.id: u for u in db.query(models.User).filter(models.User.business_id == business_id).all()}
        weeks = [self.week - timedelta(days=7), self.week, self.week + timedelta(days=7)]
        rows = db.query(models.ShiftRoster).filter(
            models.ShiftRoster.business_id == business_id,
            models.ShiftRoster.week_start.in_(weeks),
        ).all()
        self.roster: dict[tuple[int, date], str] = {}
        for r in rows:
            if week_entries is not None and r.week_start == self.week:
                continue  # replaced by the unsaved entries below
            if r.shift_type in SHIFT_WINDOWS and r.day_of_week is not None:
                self.roster[(r.user_id, r.week_start + timedelta(days=r.day_of_week))] = r.shift_type
        for e in week_entries or []:
            if not isinstance(e, dict):
                continue
            try:
                uid, day = int(e.get("user_id")), int(e.get("day"))
            except (TypeError, ValueError):
                continue
            if e.get("shift_type") in SHIFT_WINDOWS and 0 <= day <= 6 and uid in self.users:
                self.roster[(uid, self.week + timedelta(days=day))] = e["shift_type"]
        self.leaves = db.query(models.LeaveRequest).filter(
            models.LeaveRequest.business_id == business_id,
            models.LeaveRequest.status.in_(("approved", "pending")),
            models.LeaveRequest.start_date < self.week_end + timedelta(days=7),
            models.LeaveRequest.end_date >= self.week - timedelta(days=7),
        ).all()

    def leave_on(self, uid: int, d: date) -> str | None:
        status = None
        for lv in self.leaves:
            if lv.user_id == uid and lv.start_date and lv.end_date and lv.start_date <= d <= lv.end_date:
                if lv.status == "approved":
                    return "approved"
                status = "pending"
        return status

    def week_days(self, uid: int) -> list[tuple[date, str]]:
        return sorted((d, st) for (u, d), st in self.roster.items()
                      if u == uid and self.week <= d < self.week_end)

    def intervals(self, uid: int) -> list[tuple[datetime, datetime]]:
        return sorted(shift_interval(d, st) for (u, d), st in self.roster.items() if u == uid)


def _longest_weekly_rest(ctx: RosterContext, uid: int) -> tuple[bool, float]:
    """True אם יש מנוחה רצופה של 36 שעות שנופלת (לפחות 12 שעות ממנה) בתוך השבוע.
    שבוע צמוד שעוד לא סודר נחשב כמנוחה פתוחה — לא מתריעים על מה שלא ידוע."""
    ws = datetime.combine(ctx.week, time(0))
    we = ws + timedelta(days=7)
    gaps, prev_end = [], None  # None = מנוחה שמתחילה/נגמרת מחוץ לחלון הידוע
    for s, e in ctx.intervals(uid):
        gaps.append((prev_end, s))
        prev_end = e if prev_end is None else max(prev_end, e)
    gaps.append((prev_end, None))
    best = 0.0
    for gs, ge in gaps:
        lo, hi = max(gs or ws, ws), min(ge or we, we)
        overlap = (hi - lo).total_seconds() / 3600
        if overlap <= 0:
            continue
        length = float("inf") if gs is None or ge is None else (ge - gs).total_seconds() / 3600
        if length >= WEEKLY_REST_HOURS and overlap >= REST_MIN_OVERLAP_HOURS:
            return True, length
        if length != float("inf"):
            best = max(best, length)
    return False, best


def check_user(ctx: RosterContext, user: models.User) -> list[dict]:
    days = ctx.week_days(user.id)
    if not days:
        return []
    issues = []
    if not _is_schedulable(user) and user.role != "manager":
        issues.append(_issue("high", "inactive_scheduled",
                             f"{user.full_name} משובץ/ת בסידור אבל החשבון מושבת או לא אושר", user))
    week_hours = 0.0
    is_youth = False
    for d, st in days:
        hrs = shift_hours(st)
        week_hours += hrs
        lv = ctx.leave_on(user.id, d)
        if lv == "approved":
            issues.append(_issue("high", "leave_conflict",
                                 f"{user.full_name} משובץ/ת ביום {DAY_NAMES[day_index(d)]} {_ddmm(d)} אבל יש לו/ה היעדרות מאושרת", user, d))
        elif lv == "pending":
            issues.append(_issue("medium", "leave_pending",
                                 f"{user.full_name} ביקש/ה חופש ב-{_ddmm(d)} (ממתין לאישורך) ובכל זאת משובץ/ת", user, d))
        age = age_on(user.birth_date, d)
        if age is not None and age < 18:
            is_youth = True
            if age < 14:
                issues.append(_issue("high", "youth_min_age",
                                     f"{user.full_name} בן/בת {age}: אסור להעסיק מתחת לגיל 14", user, d))
            elif age < YOUTH_MIN_AGE:
                issues.append(_issue("medium", "youth_min_age",
                                     f"{user.full_name} בן/בת {age}: בגיל 14 מותר לעבוד רק בחופשת לימודים רשמית ובעבודה קלה", user, d))
            s0, s1 = shift_interval(d, st)
            fri = d - timedelta(days=(d.weekday() - 4) % 7)  # שישי של אותו סוף שבוע
            w0, w1 = shabbat_window(fri)
            if s0 < w1 and s1 > w0:
                issues.append(_issue("high", "youth_shabbat",
                                     f"{user.full_name} בן/בת {age}: משמרת {SHIFT_NAMES[st]} ב-{_ddmm(d)} נופלת בשבת. "
                                     f"אסור להעסיק נוער במנוחה השבועית, וגם היתר לא מאפשר זאת", user, d))
            if d.weekday() == 4 and hrs > YOUTH_MAX_EVE_OF_REST_HOURS:
                issues.append(_issue("high", "youth_eve_rest",
                                     f"{user.full_name} בן/בת {age}: ביום שישי נוער רשאי לעבוד עד {_fmt_h(YOUTH_MAX_EVE_OF_REST_HOURS)} שעות "
                                     f"({_fmt_h(hrs)} מתוכננות)", user, d))
            end = SHIFT_WINDOWS[st][1]
            limit = time(20, 0) if age < 16 else time(22, 0)
            if end > limit:
                extra = " (עד 23:00 רק עם היתר עבודת לילה)" if age >= 16 else ""
                issues.append(_issue("high", "youth_night",
                                     f"{user.full_name} בן/בת {age}: משמרת {SHIFT_NAMES[st]} ב-{_ddmm(d)} מסתיימת ב-{end:%H:%M}, "
                                     f"ונוער בגילו/ה רשאי לעבוד עד {limit:%H:%M}{extra}", user, d))
            if hrs > YOUTH_MAX_DAY_HOURS:
                issues.append(_issue("high", "youth_day_hours",
                                     f"{user.full_name} בן/בת {age}: {_fmt_h(hrs)} שעות ב-{_ddmm(d)}, מעל המקסימום לנוער (ככלל {_fmt_h(YOUTH_MAX_DAY_HOURS)} שעות ביום)", user, d))
        elif hrs > ADULT_MAX_DAY_HOURS:
            issues.append(_issue("medium", "long_day",
                                 f"{user.full_name}: {SHIFT_NAMES[st]} ב-{_ddmm(d)} היא {_fmt_h(hrs)} שעות, מעל {_fmt_h(ADULT_MAX_DAY_HOURS)} שעות עבודה ביום. "
                                 f"תקין רק אם יש הפסקה של 45 דק' לפחות שבה העובד/ת רשאי/ת לצאת", user, d))
    if is_youth and week_hours > YOUTH_MAX_WEEK_HOURS:
        issues.append(_issue("high", "youth_week_hours",
                             f"{user.full_name} (נוער): {_fmt_h(week_hours)} שעות מתוכננות השבוע, מעל {_fmt_h(YOUTH_MAX_WEEK_HOURS)}", user,
                             hours=round(week_hours, 1)))
    elif week_hours > MAX_WEEK_HOURS:
        issues.append(_issue("high", "week_max",
                             f"{user.full_name}: {_fmt_h(week_hours)} שעות מתוכננות השבוע, מעל המקסימום ({_fmt_h(MAX_WEEK_HOURS)} כולל שעות נוספות)", user,
                             hours=round(week_hours, 1)))
    elif week_hours > REGULAR_WEEK_HOURS:
        issues.append(_issue("info", "overtime_expected",
                             f"{user.full_name}: {_fmt_h(week_hours)} שעות מתוכננות, כלומר כ-{_fmt_h(week_hours - REGULAR_WEEK_HOURS)} שעות נוספות (125%/150%)", user,
                             hours=round(week_hours, 1)))
    ok, best = _longest_weekly_rest(ctx, user.id)
    if not ok:
        issues.append(_issue("high", "weekly_rest",
                             f"ל{user.full_name} אין 36 שעות מנוחה רצופות השבוע (הארוכה ביותר: {_fmt_h(best)} שעות). "
                             f"לפי החוק נדרשות 36 שעות רצופות (לעובד יהודי, כולל שבת, אלא אם יש היתר). "
                             f"כדאי לתת יומיים חופש צמודים או לא לשבץ בוקר אחרי ערב שלפני יום החופש", user))
    return issues


def check_week(db: Session, business_id: int, week: date, week_entries: list | None = None) -> dict:
    """בודק שבוע שלם. week_entries (אופציונלי) = סידור שעוד לא נשמר, במקום מה שבמסד."""
    ctx = RosterContext(db, business_id, week, week_entries)
    issues = []
    for uid in sorted({u for (u, d) in ctx.roster if ctx.week <= d < ctx.week_end}):
        user = ctx.users.get(uid)
        if user is not None:
            issues.extend(check_user(ctx, user))
    issues = _sort_issues(issues)
    return {"week_start": ctx.week.isoformat(), "issues": issues, "summary": _summary(issues),
            "ok": not any(i["severity"] == "high" for i in issues), "disclaimer": DISCLAIMER}


def check_hypothetical(db: Session, business_id: int, employee_id: int, d: date, shift_type: str) -> list[dict]:
    """אילו בעיות חדשות ייווצרו אם נשבץ את העובד ביום הזה (משמש את אישור ה-AI)."""
    ctx = RosterContext(db, business_id, d)
    user = ctx.users.get(int(employee_id))
    if user is None:
        return []
    before = {_issue_key(i) for i in check_user(ctx, user)}
    if shift_type in SHIFT_WINDOWS:
        ctx.roster[(user.id, d)] = shift_type
    else:
        ctx.roster.pop((user.id, d), None)
    return [i for i in check_user(ctx, user) if _issue_key(i) not in before]


def _issue_key(i: dict) -> tuple:
    # weekly issues have no date — include the hours so "62.5h -> 70h" counts as a new problem
    return (i["code"], i["date"], i.get("hours"))


# ---------------------------------------------------------------------------
# 3. מוצא מחליף
# ---------------------------------------------------------------------------
def suggest_replacements(db: Session, business_id: int, d: date, shift_type: str,
                         exclude_employee_id: int | None = None, limit: int = 5) -> dict:
    if shift_type not in SHIFT_WINDOWS:
        raise ValueError("shift_type חייב להיות morning, evening או double")
    ctx = RosterContext(db, business_id, d)
    didx = day_index(d)
    prefs = {p.user_id: p.preference for p in db.query(models.ShiftPreference).filter(
        models.ShiftPreference.business_id == business_id,
        models.ShiftPreference.week_start == ctx.week,
        models.ShiftPreference.day_of_week == didx,
    ).all()}
    candidates, skipped = [], {"working_that_day": 0, "on_leave": 0}
    for u in ctx.users.values():
        if not _is_schedulable(u) or u.id == exclude_employee_id:
            continue
        if (u.id, d) in ctx.roster:
            skipped["working_that_day"] += 1
            continue
        leave = ctx.leave_on(u.id, d)
        if leave == "approved":
            skipped["on_leave"] += 1
            continue
        score, reasons = 0.0, []
        pref = prefs.get(u.id)
        if pref == shift_type or (pref == "double" and shift_type in ("morning", "evening")):
            score += 3
            reasons.append(f"ביקש/ה לעבוד {SHIFT_NAMES[pref]} ביום הזה")
        elif pref:
            score += 1
            reasons.append(f"ביקש/ה לעבוד ביום הזה ({SHIFT_NAMES.get(pref, pref)})")
        week_hours = sum(shift_hours(st) for _, st in ctx.week_days(u.id))
        score -= week_hours / 20
        reasons.append(f"{_fmt_h(week_hours)} שעות מתוכננות השבוע" if week_hours else "עוד לא משובץ/ת השבוע")
        if leave == "pending":
            score -= 2
            reasons.append("יש לו/ה בקשת חופש ממתינה ליום הזה")
        before = {_issue_key(i) for i in check_user(ctx, u)}
        ctx.roster[(u.id, d)] = shift_type
        new_issues = [i for i in check_user(ctx, u) if _issue_key(i) not in before]
        del ctx.roster[(u.id, d)]
        blocking = [i for i in new_issues if i["severity"] == "high"]
        candidates.append({
            "employee_id": u.id, "name": u.full_name, "phone": u.phone or "",
            "score": round(score, 2), "reasons": reasons,
            "ok": not blocking, "warnings": [i["message"] for i in new_issues],
        })
    candidates.sort(key=lambda c: (not c["ok"], -c["score"], c["name"]))
    return {"date": d.isoformat(), "day": DAY_NAMES[didx], "shift_type": shift_type,
            "shift_label": SHIFT_NAMES[shift_type], "candidates": candidates[:limit],
            "total_candidates": len(candidates), "skipped": skipped}


# ---------------------------------------------------------------------------
# 2. תדריך בוקר
# ---------------------------------------------------------------------------
def _business_shifts(db: Session, business_id: int):
    return db.query(models.Shift).join(models.User, models.Shift.user_id == models.User.id).filter(
        models.User.business_id == business_id)


def daily_briefing(db: Session, business_id: int, now: datetime) -> dict:
    now = naive(now)
    today = now.date()
    ctx = RosterContext(db, business_id, today)
    users = ctx.users
    day_start = datetime.combine(today, time(0))
    today_rows = _business_shifts(db, business_id).filter(
        models.Shift.clock_in >= day_start, models.Shift.clock_in < day_start + timedelta(days=1)).all()
    clocked_today = {s.user_id for s in today_rows}
    open_shifts = _business_shifts(db, business_id).filter(models.Shift.clock_out == None).all()  # noqa: E711
    rostered_today = sorted(((uid, st) for (uid, d), st in ctx.roster.items() if d == today),
                            key=lambda x: SHIFT_WINDOWS[x[1]][0])
    rostered_ids = {uid for uid, _ in rostered_today}
    items = []
    # replacements assigned today get their grace period from the moment they were assigned
    assigned_at: dict[int, datetime] = {}
    for a in db.query(models.AuditLog).filter(
            models.AuditLog.business_id == business_id, models.AuditLog.action == "roster.assign",
            models.AuditLog.entity_id == today.isoformat()).all():
        try:
            assigned_at[int(json.loads(a.after or "{}").get("employee_id"))] = a.created_at
        except (TypeError, ValueError):
            pass

    for uid, st in rostered_today:
        u = users.get(uid)
        if u is None or uid in clocked_today or not _is_schedulable(u) or ctx.leave_on(uid, today) == "approved":
            continue
        start, _ = shift_interval(today, st)
        if uid in assigned_at and assigned_at[uid] and assigned_at[uid] > start:
            start = assigned_at[uid]
        if now >= start + LATE_GRACE:
            late_min = int((now - start).total_seconds() // 60)
            items.append({"severity": "high", "code": "no_show",
                          "title": f"{u.full_name} לא החתים/ה כניסה",
                          "detail": f"שובץ/ה ל{SHIFT_NAMES[st]} שהתחילה ב-{start:%H:%M} (לפני {late_min} דק')",
                          "employee_id": uid,
                          "action": {"type": "find_replacement", "date": today.isoformat(), "shift_type": st, "employee_id": uid}})

    for s in open_shifts:
        u = users.get(s.user_id)
        cin = naive(s.clock_in)
        if u is None or cin is None:
            continue
        hrs = (now - cin).total_seconds() / 3600
        if hrs >= FORGOT_CLOCK_OUT_HOURS:
            items.append({"severity": "high", "code": "forgot_clock_out",
                          "title": f"{u.full_name} כנראה שכח/ה להחתים יציאה",
                          "detail": f"המשמרת פתוחה מ-{cin:%d/%m %H:%M} ({_fmt_h(hrs)} שעות). צריך לסגור אותה ידנית לפני השכר",
                          "employee_id": u.id, "shift_id": s.id, "action": {"type": "tab", "tab": "attendance"}})
        elif hrs >= LONG_OPEN_SHIFT_HOURS:
            items.append({"severity": "medium", "code": "long_shift",
                          "title": f"{u.full_name} במשמרת כבר {_fmt_h(hrs)} שעות",
                          "detail": "מעבר ל-8.6 שעות ביום: שעות נוספות (125% לשעתיים הראשונות, 150% אחריהן)", "employee_id": u.id, "shift_id": s.id,
                          "action": {"type": "tab", "tab": "attendance"}})
        elif rostered_today and cin.date() == today and s.user_id not in rostered_ids and u.role != "manager":
            items.append({"severity": "info", "code": "unscheduled",
                          "title": f"{u.full_name} החתים/ה כניסה בלי להיות בסידור",
                          "detail": f"נכנס/ה ב-{cin:%H:%M}", "employee_id": u.id})

    pending_leave = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.business_id == business_id, models.LeaveRequest.status == "pending").count()
    if pending_leave:
        items.append({"severity": "medium", "code": "pending_leave",
                      "title": f"{pending_leave} בקשות חופש ממתינות לאישורך", "detail": "",
                      "action": {"type": "tab", "tab": "leave"}})
    pending_regs = sum(1 for u in users.values() if u.approval_status == "pending")
    if pending_regs:
        items.append({"severity": "medium", "code": "pending_registration",
                      "title": f"{pending_regs} עובדים חדשים מחכים לאישור הצטרפות", "detail": "",
                      "action": {"type": "tab", "tab": "employees"}})

    week_check = []
    for uid in sorted({u for (u, d) in ctx.roster if ctx.week <= d < ctx.week_end}):
        if uid in users:
            week_check.extend(check_user(ctx, users[uid]))
    highs = [i for i in week_check if i["severity"] == "high"]
    if highs:
        items.append({"severity": "high", "code": "compliance",
                      "title": f"{len(highs)} בעיות חוקיות בסידור של השבוע",
                      "detail": " · ".join(i["message"] for i in highs[:2]) + (" ועוד" if len(highs) > 2 else ""),
                      "action": {"type": "tab", "tab": "roster"}})

    # תחזית שעות לשבוע: מה שכבר עבדו + מה שעוד מתוכנן
    week_start_dt = datetime.combine(ctx.week, time(0))
    worked: dict[int, float] = {}
    for s in _business_shifts(db, business_id).filter(models.Shift.clock_in >= week_start_dt).all():
        cin = naive(s.clock_in)
        if cin is None:
            continue
        if s.clock_out is None and (now - cin).total_seconds() / 3600 >= FORGOT_CLOCK_OUT_HOURS:
            continue  # already reported as forgot_clock_out — don't count bogus hours as overtime
        end = naive(s.clock_out) or now
        worked[s.user_id] = worked.get(s.user_id, 0.0) + max(0.0, (end - cin).total_seconds() / 3600)
    projected = []
    for uid, u in users.items():
        if u.role == "manager":
            continue
        remaining = sum(shift_hours(st) for d, st in ctx.week_days(uid)
                        if d > today or (d == today and uid not in clocked_today))
        total = worked.get(uid, 0.0) + remaining
        if total > REGULAR_WEEK_HOURS:
            projected.append((total, u.full_name))
    if projected:
        projected.sort(reverse=True)
        items.append({"severity": "medium", "code": "overtime_projection",
                      "title": f"{len(projected)} עובדים צפויים לשעות נוספות השבוע",
                      "detail": ", ".join(f"{n} ({_fmt_h(h)} ש')" for h, n in projected[:4]),
                      "action": {"type": "tab", "tab": "roster"}})

    items.sort(key=lambda i: SEVERITY_ORDER.get(i["severity"], 9))
    n_urgent = sum(1 for i in items if i["severity"] == "high")
    counts = {"morning": 0, "evening": 0, "double": 0}
    for _, st in rostered_today:
        counts[st] += 1
    if n_urgent:
        headline = f"{n_urgent} דברים דורשים טיפול עכשיו"
    elif items:
        headline = "אין בעיות דחופות. יש כמה דברים שכדאי לעבור עליהם"
    else:
        headline = "הכל תקין היום"
    return {
        "date": today.isoformat(), "day": DAY_NAMES[day_index(today)], "generated_at": now.isoformat(timespec="minutes"),
        "headline": headline,
        "stats": {"rostered_today": len(rostered_today), "morning": counts["morning"] + counts["double"],
                  "evening": counts["evening"] + counts["double"], "on_shift_now": len(open_shifts),
                  "pending_leave": pending_leave, "pending_registrations": pending_regs},
        "items": items,
    }


# ---------------------------------------------------------------------------
# 4. מבקר טרום-שכר
# ---------------------------------------------------------------------------
def _overlap_hours(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 3600)


def _shabbat_hours(cin: datetime, cout: datetime) -> float:
    """שעות בשבת בקירוב (משקיעה בשישי עד צאת השבת), לפי זמני שקיעה משוערים לחודש."""
    total, d = 0.0, cin.date() - timedelta(days=1)
    while d <= cout.date():
        if d.weekday() == 4:  # שישי
            total += _overlap_hours(cin, cout, *shabbat_window(d))
        d += timedelta(days=1)
    return total


def payroll_audit(db: Session, business_id: int, year: int, month: int, now: datetime) -> dict:
    now = naive(now)
    month_start = date(year, month, 1)
    month_end = date(year + (month == 12), month % 12 + 1, 1)
    users = {u.id: u for u in db.query(models.User).filter(models.User.business_id == business_id).all()}
    shifts = _business_shifts(db, business_id).filter(
        models.Shift.clock_in >= datetime.combine(month_start, time(0)),
        models.Shift.clock_in < datetime.combine(month_end, time(0)),
    ).order_by(models.Shift.user_id, models.Shift.clock_in).all()
    issues = []
    by_user: dict[int, list] = {}
    for s in shifts:
        by_user.setdefault(s.user_id, []).append(s)

    week_hours: dict[tuple[int, date], float] = {}
    shabbat: dict[int, float] = {}
    for uid, rows in by_user.items():
        u = users.get(uid)
        if u is None:
            continue
        prev_end = None
        for s in rows:
            cin, cout = naive(s.clock_in), naive(s.clock_out)
            if cout is None:
                hrs = (now - cin).total_seconds() / 3600
                if hrs >= FORGOT_CLOCK_OUT_HOURS:
                    issues.append(_issue("high", "open_shift",
                                         f"{u.full_name}: משמרת מ-{cin:%d/%m %H:%M} עדיין פתוחה ({_fmt_h(hrs)} שעות). צריך לסגור לפני חישוב השכר",
                                         u, cin.date(), shift_id=s.id))
                continue
            hrs = (cout - cin).total_seconds() / 3600
            if prev_end and cin < prev_end:
                issues.append(_issue("high", "overlap",
                                     f"{u.full_name}: שתי משמרות חופפות ב-{cin:%d/%m}. כנראה החתמה כפולה, השעות נספרות פעמיים",
                                     u, cin.date(), shift_id=s.id))
            prev_end = max(prev_end, cout) if prev_end else cout
            if hrs > MAX_ACTUAL_SHIFT_HOURS:
                issues.append(_issue("medium", "very_long_shift",
                                     f"{u.full_name}: {_fmt_h(hrs)} שעות ב-{cin:%d/%m} ({cin:%H:%M}–{cout:%H:%M}). לוודא שזה נכון ושהשעות הנוספות משולמות",
                                     u, cin.date(), shift_id=s.id))
            elif hrs < 0.25:
                issues.append(_issue("info", "very_short_shift",
                                     f"{u.full_name}: משמרת של {int(hrs * 60)} דקות ב-{cin:%d/%m}. אולי החתמה בטעות",
                                     u, cin.date(), shift_id=s.id))
            wk = (uid, week_start_of(cin.date()))
            week_hours[wk] = week_hours.get(wk, 0.0) + hrs
            sh = _shabbat_hours(cin, cout)
            if sh > 0:
                shabbat[uid] = shabbat.get(uid, 0.0) + sh

    for (uid, wk), hrs in sorted(week_hours.items(), key=lambda x: x[0][1]):
        u = users[uid]
        if hrs > MAX_WEEK_HOURS:
            issues.append(_issue("high", "week_over_max",
                                 f"{u.full_name}: {_fmt_h(hrs)} שעות בשבוע של {_ddmm(wk)}, מעל המקסימום החוקי ({_fmt_h(MAX_WEEK_HOURS)})", u, wk))
        elif hrs > REGULAR_WEEK_HOURS:
            issues.append(_issue("info", "week_overtime",
                                 f"{u.full_name}: {_fmt_h(hrs - REGULAR_WEEK_HOURS)} שעות נוספות שבועיות בשבוע של {_ddmm(wk)}. "
                                 f"החישוב האוטומטי סופר שעות נוספות רק לפי יום, כדאי לוודא מול רו\"ח", u, wk))

    for uid, hrs in shabbat.items():
        u = users[uid]
        issues.append(_issue("info", "shabbat_hours",
                             f"{u.full_name}: כ-{_fmt_h(hrs)} שעות בשבת החודש (חישוב משוער). על שעות במנוחה השבועית משלמים לפחות 150% מהשכר הרגיל "
                             f"(שעות נוספות בשבת: 175%/200%). זה לא נכלל בשכר המשוער",
                             u, hours=round(hrs, 1)))

    for uid in by_user:
        u = users.get(uid)
        if u is None or u.role == "manager":
            continue
        rate = u.hourly_rate or 0
        age = age_on(u.birth_date, month_start)
        general_min = min_wage_on(month_start)
        if rate <= 0:
            issues.append(_issue("medium", "no_rate", f"{u.full_name}: אין תעריף שעתי, ולכן השכר שלו/ה לא מחושב", u))
        elif age is not None and age < 18:
            youth_min = YOUTH_MIN_WAGE.get(max(age, 14), YOUTH_MIN_WAGE[14])
            if rate < youth_min:
                issues.append(_issue("high", "below_youth_min_wage",
                                     f"{u.full_name} (בן/בת {age}): תעריף {_fmt_h(rate)} ₪ לשעה, מתחת לשכר המינימום לנוער בגילו/ה "
                                     f"({youth_min:.2f} ₪). לחניך/ה התעריף שונה", u))
        elif rate < general_min:
            issues.append(_issue("high", "below_min_wage",
                                 f"{u.full_name}: תעריף {_fmt_h(rate)} ₪ לשעה, מתחת לשכר המינימום ({general_min:.2f} ₪)", u))
        elif CATERING_MIN_WAGE_HOURLY and rate < CATERING_MIN_WAGE_HOURLY:
            issues.append(_issue("medium", "below_catering_wage",
                                 f"{u.full_name}: תעריף {_fmt_h(rate)} ₪ לשעה. בקבלני הסעדה מוסדית (לא מסעדות) חל צו הרחבה: "
                                 f"מינימום {CATERING_MIN_WAGE_HOURLY:.0f} ₪ לשעה", u))

    # סידור מול נוכחות (רק לימים שעברו, ורק בשבועות שבהם העסק עבד עם סידור)
    roster_rows = db.query(models.ShiftRoster).filter(
        models.ShiftRoster.business_id == business_id,
        models.ShiftRoster.week_start >= month_start - timedelta(days=6),
        models.ShiftRoster.week_start < month_end,
    ).all()
    worked_days = {(s.user_id, naive(s.clock_in).date()) for s in shifts}
    approved_leaves = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.business_id == business_id, models.LeaveRequest.status == "approved",
        models.LeaveRequest.start_date < month_end, models.LeaveRequest.end_date >= month_start).all()

    def on_leave(uid, d):
        return any(lv.user_id == uid and lv.start_date <= d <= lv.end_date for lv in approved_leaves)
    last_day = min(month_end, now.date())
    for r in roster_rows:
        d = r.week_start + timedelta(days=r.day_of_week or 0)
        u = users.get(r.user_id)
        if u is None or r.shift_type not in SHIFT_WINDOWS or not (month_start <= d < last_day):
            continue
        if (r.user_id, d) not in worked_days and not on_leave(r.user_id, d):
            issues.append(_issue("medium", "rostered_no_attendance",
                                 f"{u.full_name}: שובץ/ה ל{SHIFT_NAMES[r.shift_type]} ב-{_ddmm(d)} ואין החתמה. העובד/ת לא הגיע/ה או שכח/ה להחתים",
                                 u, d))

    issues = _sort_issues(issues)
    total_hours = sum(week_hours.values())
    return {"year": year, "month": month, "issues": issues, "summary": _summary(issues),
            "shifts_checked": len(shifts), "total_hours": round(total_hours, 1),
            "ok": not any(i["severity"] == "high" for i in issues), "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# 5. יומן שינויים (audit log) — כל רשומה חתומה ב-hash של הקודמת לה
# ---------------------------------------------------------------------------
def _row_hash(row: "models.AuditLog") -> str:
    payload = json.dumps([
        row.business_id, row.actor_id, row.actor_name, row.action, row.entity, row.entity_id,
        row.summary, row.before, row.after, row.source,
        row.created_at.isoformat() if row.created_at else None, row.prev_hash,
    ], ensure_ascii=False, sort_keys=False)
    # HMAC with the server secret: someone with DB access can't recompute the chain after editing a row
    return hmac.new(_AUDIT_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


_AUDIT_KEY = (os.getenv("SECRET_KEY") or "dev-audit-key").encode("utf-8")


def _jsonable(v):
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str, sort_keys=True)


def log_audit(db: Session, business_id: int, actor, action: str, summary: str,
              entity: str | None = None, entity_id=None, before=None, after=None,
              source: str = "manual", now: datetime | None = None) -> "models.AuditLog":
    """מוסיף רשומה ליומן (בלי commit — הקורא עושה commit יחד עם השינוי עצמו)."""
    # serialize writers per business (Postgres row lock) so two requests can't fork the chain
    db.query(models.Business.id).filter(models.Business.id == business_id).with_for_update().first()
    last = db.query(models.AuditLog).filter(models.AuditLog.business_id == business_id)\
        .order_by(models.AuditLog.id.desc()).first()
    actor_id = getattr(actor, "id", None) if actor is not None else None
    actor_name = getattr(actor, "full_name", None) if actor is not None else None
    row = models.AuditLog(
        business_id=business_id, actor_id=actor_id, actor_name=actor_name or "מערכת",
        action=action, entity=entity, entity_id=str(entity_id) if entity_id is not None else None,
        summary=summary, before=_jsonable(before), after=_jsonable(after), source=source,
        created_at=naive(now or datetime.utcnow()).replace(microsecond=0),
        prev_hash=last.hash if last else "",
    )
    row.hash = _row_hash(row)
    db.add(row)
    return row


def list_audit(db: Session, business_id: int, limit: int = 100, entity: str | None = None) -> list[dict]:
    q = db.query(models.AuditLog).filter(models.AuditLog.business_id == business_id)
    if entity:
        q = q.filter(models.AuditLog.entity == entity)
    rows = q.order_by(models.AuditLog.id.desc()).limit(max(1, min(int(limit), 500))).all()
    return [{
        "id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
        "actor": r.actor_name, "action": r.action, "entity": r.entity, "entity_id": r.entity_id,
        "summary": r.summary, "source": r.source,
        "before": json.loads(r.before) if r.before else None,
        "after": json.loads(r.after) if r.after else None,
    } for r in rows]


def verify_chain(db: Session, business_id: int) -> dict:
    rows = db.query(models.AuditLog).filter(models.AuditLog.business_id == business_id)\
        .order_by(models.AuditLog.id).all()
    prev = ""
    for r in rows:
        if r.prev_hash != prev or r.hash != _row_hash(r):
            return {"ok": False, "count": len(rows), "broken_at": r.id,
                    "message": f"היומן שונה מחוץ למערכת (רשומה {r.id}). השרשרת לא תקינה"}
        prev = r.hash
    return {"ok": True, "count": len(rows), "broken_at": None,
            "message": f"כל {len(rows)} הרשומות ביומן תקינות ולא שונו"}
