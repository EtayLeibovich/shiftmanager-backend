"""
Run this script once to apply database migrations for new columns and tables.
Safe to run multiple times (idempotent).
Usage: python migrate.py
"""
from sqlalchemy import create_engine, text

SQLALCHEMY_DATABASE_URL = "sqlite:///./shift_manager.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})

ALTER_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN hourly_rate REAL DEFAULT 0.0",
    "ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN branch_id INTEGER",
    "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
]

CREATE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS branches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        name TEXT NOT NULL,
        address TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        start_time DATETIME NOT NULL,
        end_time DATETIME NOT NULL,
        notes TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leave_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        leave_type TEXT DEFAULT 'vacation',
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        reason TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        manager_note TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        user_id INTEGER REFERENCES users(id),
        type TEXT NOT NULL,
        message TEXT NOT NULL,
        is_read INTEGER DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


def run():
    with engine.connect() as conn:
        print("Running ALTER TABLE migrations...")
        for stmt in ALTER_MIGRATIONS:
            try:
                conn.execute(text(stmt))
                conn.commit()
                print(f"  OK: {stmt[:60]}...")
            except Exception as e:
                if "duplicate column name" in str(e).lower():
                    print(f"  SKIP (already exists): {stmt[25:60]}...")
                else:
                    print(f"  ERROR: {e}")

        print("\nCreating new tables...")
        for stmt in CREATE_TABLES:
            try:
                conn.execute(text(stmt))
                conn.commit()
                table_name = [w for w in stmt.split() if w not in ("CREATE", "TABLE", "IF", "NOT", "EXISTS")][0]
                print(f"  OK: {table_name}")
            except Exception as e:
                print(f"  ERROR: {e}")

    print("\nMigrations complete!")


if __name__ == "__main__":
    run()
