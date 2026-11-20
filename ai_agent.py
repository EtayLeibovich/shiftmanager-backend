"""
סוכן AI למנהל — tool calling דרך Groq (פורמט OpenAI).

כלי קריאה (READ_TOOLS) רצים מיד ומחזירים נתונים למודל.
כלי כתיבה (WRITE_TOOLS) לא מבוצעים ע"י המודל: הם עוברים ולידציה ומוחזרים
לממשק כ"פעולה ממתינה". רק אחרי שהמנהל לוחץ "אשר" הממשק קורא ל-
/ai/execute-action, שמריץ את execute_action().
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import extract
from sqlalchemy.orm import Session

import models

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
MODEL = "llama-3.3-70b-versatile"
MAX_STEPS = 6

DAY_NAMES = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
SHIFT_NAMES = {"morning": "בוקר", "evening": "ערב", "double": "כפולה", "off": "חופש"}


class ToolError(Exception):
    """שגיאה שמוחזרת למודל כדי שיתקן את עצמו."""


# ---------------------------------------------------------------------------
# עזרים
# ---------------------------------------------------------------------------
def _today() -> date:
    return datetime.now(ISRAEL_TZ).date()


def _week_start_and_day(d: date) -> tuple[date, int]:
    """השבוע מתחיל ביום ראשון. מחזיר (תאריך יום ראשון, אינדקס יום 0=ראשון)."""
    day_idx = (d.weekday() + 1) % 7  # Python: Monday=0 → Sunday=0
    return d - timedelta(days=day_idx), day_idx


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ToolError(f"תאריך לא תקין: {s!r}. השתמש בפורמט YYYY-MM-DD.")


def _get_employee(db: Session, business_id: int, employee_id) -> models.User:
    emp = db.query(models.User).filter(
        models.User.id == int(employee_id),
        models.User.business_id == business_id,
    ).first()
    if not emp:
        raise ToolError(f"עובד {employee_id} לא נמצא בעסק. השתמש ב-list_employees.")
    return emp


def _get_leave(db: Session, business_id: int, leave_id) -> models.LeaveRequest:
    leave = db.query(models.LeaveRequest).filter(
        models.LeaveRequest.id == int(leave_id),
        models.LeaveRequest.business_id == business_id,
    ).first()
    if not leave:
        raise ToolError(f"בקשת היעדרות {leave_id} לא נמצאה. השתמש ב-list_leave_requests.")
    return leave


# ---------------------------------------------------------------------------
# כלי קריאה
# ---------------------------------------------------------------------------
def list_employees(db, business_id):
    workers = db.query(models.User).filter(
        models.User.business_id == business_id,
        models.User.approval_status != "rejected",
    ).all()
    return [
        {"id": w.id, "name": w.full_name, "role": w.role,
         "hourly_rate": w.hourly_rate or 0, "active": w.is_active is not False}
        for w in workers
    ]


def get_week_roster(db, business_id, week_start=None):
    d = _parse_date(week_start) if week_start else _today()
    week, _ = _week_start_and_day(d)
    entries = db.query(models.ShiftRoster, models.User).join(
        models.User, models.ShiftRoster.user_id == models.User.id
    ).filter(
        models.ShiftRoster.business_id == business_id,
        models.ShiftRoster.week_start == week,
    ).all()
    return {
        "week_start": week.isoformat(),
        "entries": [
            {"employee_id": e.User.id, "name": e.User.full_name,
             "date": (week + timedelta(days=e.ShiftRoster.day_of_week)).isoformat(),
             "day": DAY_NAMES[e.ShiftRoster.day_of_week],
             "shift_type": e.ShiftRoster.shift_type,
             "position": e.ShiftRoster.position or ""}
            for e in entries
        ],
    }


def get_shift_preferences(db, business_id, week_start=None):
    d = _parse_date(week_start) if week_start else _today()
    week, _ = _week_start_and_day(d)
    prefs = db.query(models.ShiftPreference, models.User).join(
        models.User, models.ShiftPreference.user_id == models.User.id
    ).filter(
        models.ShiftPreference.business_id == business_id,
        models.ShiftPreference.week_start == week,
    ).all()
    return {
        "week_start": week.isoformat(),
        "preferences": [
            {"employee_id": p.User.id, "name": p.User.full_name,
             "date": (week + timedelta(days=p.ShiftPreference.day_of_week)).isoformat(),
             "day": DAY_NAMES[p.ShiftPreference.day_of_week],
             "preference": p.ShiftPreference.preference}
            for p in prefs
        ],
    }


def list_leave_requests(db, business_id, status=None):
    q = db.query(models.LeaveRequest, models.User).join(
        models.User, models.LeaveRequest.user_id == models.User.id
    ).filter(models.LeaveRequest.business_id == business_id)
    if status:
        q = q.filter(models.LeaveRequest.status == status)
    rows = q.order_by(models.LeaveRequest.start_date.desc()).limit(30).all()
    return [
        {"leave_id": r.LeaveRequest.id, "employee_id": r.User.id, "name": r.User.full_name,
         "type": r.LeaveRequest.leave_type, "status": r.LeaveRequest.status,
         "start": str(r.LeaveRequest.start_date), "end": str(r.LeaveRequest.end_date),
         "reason": r.LeaveRequest.reason or ""}
        for r in rows
    ]


def get_active_workers(db, business_id):
    rows = db.query(models.Shift, models.User).join(models.User).filter(
        models.User.business_id == business_id,
        models.Shift.clock_out == None,  # noqa: E711
    ).all()
    return [
        {"employee_id": r.User.id, "name": r.User.full_name,
         "clock_in": r.Shift.clock_in.strftime("%d/%m/%Y %H:%M") if r.Shift.clock_in else None}
        for r in rows
    ]


def get_hours_report(db, business_id, year=None, month=None, calc_salary=None):
    today = _today()
    year, month = int(year or today.year), int(month or today.month)
    rows = []
    for w in db.query(models.User).filter(models.User.business_id == business_id).all():
        shifts = db.query(models.Shift).filter(
            models.Shift.user_id == w.id,
            extract("year", models.Shift.clock_in) == year,
            extract("month", models.Shift.clock_in) == month,
            models.Shift.clock_out != None,  # noqa: E711
        ).all()
        hours = sum(s.total_hours or 0 for s in shifts)
        salary = sum(calc_salary(s.total_hours, w.hourly_rate or 0) for s in shifts if s.total_hours) if calc_salary else None
        rows.append({"name": w.full_name, "shifts": len(shifts),
                     "hours": round(hours, 2), "estimated_salary": round(salary or 0, 2)})
    return {"year": year, "month": month, "employees": rows}


# ---------------------------------------------------------------------------
# כלי כתיבה — validate_* בונה תיאור לאישור, _exec_* מבצע בפועל
# ---------------------------------------------------------------------------
def _validate_set_roster_shift(db, business_id, args):
    emp = _get_employee(db, business_id, args.get("employee_id"))
    d = _parse_date(args.get("date"))
    shift_type = args.get("shift_type")
    if shift_type not in SHIFT_NAMES:
        raise ToolError("shift_type חייב להיות אחד מ: morning, evening, double, off")
    _, day_idx = _week_start_and_day(d)
    pos = f" ({args['position']})" if args.get("position") else ""
    return f"שיבוץ {emp.full_name} ביום {DAY_NAMES[day_idx]} {d.strftime('%d/%m')}: {SHIFT_NAMES[shift_type]}{pos}"


def _exec_set_roster_shift(db, business_id, args):
    emp = _get_employee(db, business_id, args["employee_id"])
    week, day_idx = _week_start_and_day(_parse_date(args["date"]))
    existing = db.query(models.ShiftRoster).filter(
        models.ShiftRoster.business_id == business_id,
        models.ShiftRoster.user_id == emp.id,
        models.ShiftRoster.week_start == week,
        models.ShiftRoster.day_of_week == day_idx,
    ).first()
    if args["shift_type"] == "off":
        if existing:
            db.delete(existing)
    elif existing:
        existing.shift_type = args["shift_type"]
        existing.position = args.get("position") or existing.position
    else:
        db.add(models.ShiftRoster(
            business_id=business_id, user_id=emp.id, week_start=week,
            day_of_week=day_idx, shift_type=args["shift_type"],
            position=args.get("position") or None,
        ))
    db.commit()


def _validate_update_leave_status(db, business_id, args):
    leave = _get_leave(db, business_id, args.get("leave_id"))
    if args.get("status") not in ("approved", "rejected"):
        raise ToolError("status חייב להיות approved או rejected")
    emp = db.get(models.User, leave.user_id)
    verb = "אישור" if args["status"] == "approved" else "דחיית"
    return f"{verb} בקשת היעדרות של {emp.full_name if emp else '?'} ({leave.start_date} עד {leave.end_date})"


def _exec_update_leave_status(db, business_id, args, notify):
    leave = _get_leave(db, business_id, args["leave_id"])
    leave.status = args["status"]
    leave.manager_note = args.get("note") or ""
    db.commit()
    status_text = "אושרה ✅" if args["status"] == "approved" else "נדחתה ❌"
    notify(db, business_id, f"leave_{args['status']}",
           f"בקשת ההיעדרות שלך מ-{leave.start_date} עד {leave.end_date} {status_text}",
           user_id=leave.user_id)


def _validate_notify_employee(db, business_id, args):
    emp = _get_employee(db, business_id, args.get("employee_id"))
    msg = (args.get("message") or "").strip()
    if not msg:
        raise ToolError("message ריק")
    return f"שליחת הודעה ל{emp.full_name}: \"{msg}\""


def _exec_notify_employee(db, business_id, args, notify):
    emp = _get_employee(db, business_id, args["employee_id"])
    notify(db, business_id, "manager_message", args["message"].strip(), user_id=emp.id)


# ---------------------------------------------------------------------------
# הגדרות כלים למודל
# ---------------------------------------------------------------------------
def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
    }}


TOOL_DEFS = [
    _fn("list_employees", "רשימת כל העובדים בעסק עם id, תפקיד ותעריף."),
    _fn("get_week_roster", "סידור העבודה לשבוע שמכיל את התאריך הנתון.",
        {"week_start": {"type": "string", "description": "כל תאריך בשבוע, YYYY-MM-DD. ברירת מחדל: השבוע הנוכחי"}}),
    _fn("get_shift_preferences", "העדפות המשמרת שהעובדים הגישו לשבוע.",
        {"week_start": {"type": "string", "description": "כל תאריך בשבוע, YYYY-MM-DD"}}),
    _fn("list_leave_requests", "בקשות היעדרות/חופשה.",
        {"status": {"type": "string", "enum": ["pending", "approved", "rejected"]}}),
    _fn("get_active_workers", "מי נמצא כרגע במשמרת (החתים כניסה ולא יציאה)."),
    _fn("get_hours_report", "דוח שעות ושכר משוער לחודש.",
        {"year": {"type": "integer"}, "month": {"type": "integer"}}),
    _fn("set_roster_shift", "שבץ עובד למשמרת בסידור ביום מסוים (דורש אישור מנהל). off = הסר שיבוץ.",
        {"employee_id": {"type": "integer"},
         "date": {"type": "string", "description": "YYYY-MM-DD"},
         "shift_type": {"type": "string", "enum": ["morning", "evening", "double", "off"]},
         "position": {"type": "string", "description": "עמדה, אופציונלי (למשל מטבח, פס, גריל)"}},
        ["employee_id", "date", "shift_type"]),
    _fn("update_leave_status", "אשר או דחה בקשת היעדרות (דורש אישור מנהל).",
        {"leave_id": {"type": "integer"},
         "status": {"type": "string", "enum": ["approved", "rejected"]},
         "note": {"type": "string"}},
        ["leave_id", "status"]),
    _fn("notify_employee", "שלח הודעה לעובד באפליקציה (דורש אישור מנהל).",
        {"employee_id": {"type": "integer"}, "message": {"type": "string"}},
        ["employee_id", "message"]),
]

READ_TOOLS = {
    "list_employees": list_employees,
    "get_week_roster": get_week_roster,
    "get_shift_preferences": get_shift_preferences,
    "list_leave_requests": list_leave_requests,
    "get_active_workers": get_active_workers,
    "get_hours_report": get_hours_report,
}

WRITE_TOOLS = {
    "set_roster_shift": (_validate_set_roster_shift, _exec_set_roster_shift),
    "update_leave_status": (_validate_update_leave_status, _exec_update_leave_status),
    "notify_employee": (_validate_notify_employee, _exec_notify_employee),
}


# ---------------------------------------------------------------------------
# API ציבורי
# ---------------------------------------------------------------------------
def run_agent(client, db: Session, business_id: int, query: str, history: list, calc_salary=None) -> dict:
    today = _today()
    system = (
        "אתה סוכן AI של מנהל עסק לניהול משמרות. ענה תמיד בעברית, בקצרה ולעניין.\n"
        f"היום: יום {DAY_NAMES[_week_start_and_day(today)[1]]}, {today.isoformat()}. השבוע מתחיל ביום ראשון.\n"
        "השתמש בכלים כדי לשלוף נתונים — אל תמציא נתונים. כדי לשבץ/לאשר/לשלוח הודעה, "
        "קודם מצא את ה-id המתאים (list_employees / list_leave_requests) ואז קרא לכלי הכתיבה. "
        "פעולות כתיבה לא מבוצעות מיד — הן ממתינות לאישור המנהל, אז אמור לו לאשר אותן בכפתורים."
    )
    messages = [{"role": "system", "content": system}]
    for m in history[-20:]:
        role = "assistant" if m.get("role") in ("model", "assistant") else "user"
        messages.append({"role": role, "content": str(m.get("content", ""))})
    messages.append({"role": "user", "content": query})

    pending = []
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOL_DEFS,
            tool_choice="auto", max_tokens=1024, temperature=0.2,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return {"answer": msg.content or "", "pending_actions": pending}

        messages.append({
            "role": "assistant", "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}") or {}
                if name in READ_TOOLS:
                    if name == "get_hours_report":
                        args["calc_salary"] = calc_salary
                    result = READ_TOOLS[name](db, business_id, **args)
                elif name in WRITE_TOOLS:
                    desc = WRITE_TOOLS[name][0](db, business_id, args)
                    pending.append({"tool": name, "args": args, "description": desc})
                    result = {"status": "ממתין לאישור המנהל", "action": desc}
                else:
                    result = {"error": f"כלי לא קיים: {name}"}
            except ToolError as e:
                result = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 — מחזירים למודל, לא מפילים את הבקשה
                result = {"error": f"{type(e).__name__}: {e}"}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})

    return {"answer": "הגעתי למגבלת הצעדים. נסה לנסח את הבקשה בצורה ממוקדת יותר.",
            "pending_actions": pending}


def execute_action(db: Session, business_id: int, tool: str, args: dict, notify) -> str:
    """מבצע פעולת כתיבה אחרי אישור המנהל. זורק ToolError על קלט לא תקין."""
    if tool not in WRITE_TOOLS:
        raise ToolError(f"פעולה לא מוכרת: {tool}")
    validate, run = WRITE_TOOLS[tool]
    desc = validate(db, business_id, args)
    if tool == "set_roster_shift":
        run(db, business_id, args)
    else:
        run(db, business_id, args, notify)
    return desc
