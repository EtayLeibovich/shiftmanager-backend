import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# בסביבת production (Render) משתמש ב-DATABASE_URL, אחרת SQLite מקומי
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shift_manager.db")

# Render נותן postgres:// אבל SQLAlchemy צריך postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

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
