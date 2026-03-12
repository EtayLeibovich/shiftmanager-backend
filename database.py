import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# בסביבת production (Render) משתמש ב-DATABASE_URL, אחרת SQLite מקומי
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shift_manager.db")

# Render נותן postgres:// אבל SQLAlchemy צריך postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

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
        db.close()
