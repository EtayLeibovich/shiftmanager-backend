from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# הגדרת נתיב למסד הנתונים. אנחנו נשתמש ב-SQLite שייצור קובץ מקומי בתיקייה שלנו
SQLALCHEMY_DATABASE_URL = "sqlite:///./shift_manager.db"

# יצירת המנוע שמתחבר למסד הנתונים
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# "מפעל" הסשנים שלנו. דרכו נבקש גישה לנתונים
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# מחלקה בסיסית שממנה נבנה את כל הטבלאות
Base = declarative_base()

# ==========================================
# מנגנון ניהול משאבים (Memory & Connection Management)
# ==========================================
def get_db():
    db = SessionLocal()
    try:
        # מספקים את החיבור לשרת לצורך ביצוע פעולות (קריאה/כתיבה)
        yield db
    finally:
        # הקסם קורה כאן: ברגע שהפעולה מסתיימת, החיבור נסגר ומשוחרר מהזיכרון בוודאות מוחלטת!
        db.close()