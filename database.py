import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# בסביבת production (Render) משתמש ב-DATABASE_URL, אחרת SQLite מקומי
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shift_manager.db")

# Render/Neon נותנים postgres:// או postgresql://. מציינים במפורש את הדרייבר psycopg2
# (SQLAlchemy 2.1 עבר כברירת מחדל ל-psycopg 3, שלא מותקן אצלנו).
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_prefix):]
        break

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,      # בודק שהחיבור חי לפני שימוש
        pool_size=5,             # מקסימום 5 חיבורים פעילים במקביל
        max_overflow=10,         # עד 10 חיבורים נוספים בעומס זמני
        pool_timeout=30,         # זמן המתנה מקסימלי לחיבור פנוי (שניות)
        pool_recycle=1800,       # מחזיר חיבורים כל 30 דקות למנוע stale connections
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()  # מחזיר את החיבור ל-pool (לא סוגר פיזית)
