"""
Database models for The Founder's File.
SQLite via SQLAlchemy — file lives at founders-file/founders_file.db (git-ignored).
"""

import os
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    # Always set (even for Google accounts, to an unusable random hash) so this
    # stays NOT NULL — avoids a destructive SQLite column-nullability migration
    # on the existing founders_file.db. Login always checks auth_provider first.
    password_hash = db.Column(db.String(255), nullable=False)
    # "password" or "google" — which way this account signs in.
    auth_provider = db.Column(db.String(20), nullable=False, default="password")
    google_sub = db.Column(db.String(255), nullable=True, unique=True, index=True)
    has_onboarded = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    submissions = db.relationship(
        "Submission", backref="user", lazy=True, order_by="Submission.created_at.desc()"
    )

    def to_public_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "authProvider": self.auth_provider,
            "hasOnboarded": self.has_onboarded,
        }


class Submission(db.Model):
    """One founder's-file run: the idea plus whichever round results exist."""

    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    idea = db.Column(db.Text, nullable=False)
    # Case name is separate from the idea description — a short label the
    # user picks (e.g. "Riverside Coffee Cart") vs. the full prose description.
    case_name = db.Column(db.String(255), nullable=True)
    budget = db.Column(db.String(120), nullable=True)
    location = db.Column(db.String(255), nullable=True)
    analyze_result = db.Column(db.JSON, nullable=True)
    model_result = db.Column(db.JSON, nullable=True)
    tax_result = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "idea": self.idea,
            "caseName": self.case_name,
            "budget": self.budget,
            "location": self.location,
            "analyzeResult": self.analyze_result,
            "modelResult": self.model_result,
            "taxResult": self.tax_result,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }


def migrate_schema(engine):
    """Add columns introduced after the first release, without dropping data.

    SQLite's ALTER TABLE can add columns but not change nullability or add
    real constraints, so this only ever does additive, idempotent ADD COLUMN
    calls — safe to run on every startup, no-op once a column exists.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return  # fresh DB — db.create_all() already made the current shape

    existing = {col["name"] for col in inspector.get_columns("users")}
    additions = {
        "auth_provider": "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(20) NOT NULL DEFAULT 'password'",
        "google_sub": "ALTER TABLE users ADD COLUMN google_sub VARCHAR(255)",
        # Existing accounts (from before onboarding existed) default to
        # already-onboarded so they don't see it retroactively; new signups
        # explicitly pass has_onboarded=False in code.
        "has_onboarded": "ALTER TABLE users ADD COLUMN has_onboarded BOOLEAN NOT NULL DEFAULT 1",
    }
    with engine.begin() as conn:
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(text(ddl))

    if "submissions" in inspector.get_table_names():
        existing_sub = {col["name"] for col in inspector.get_columns("submissions")}
        sub_additions = {
            "case_name": "ALTER TABLE submissions ADD COLUMN case_name VARCHAR(255)",
            "budget": "ALTER TABLE submissions ADD COLUMN budget VARCHAR(120)",
            "location": "ALTER TABLE submissions ADD COLUMN location VARCHAR(255)",
        }
        with engine.begin() as conn:
            for column, ddl in sub_additions.items():
                if column not in existing_sub:
                    conn.execute(text(ddl))


def backup_database():
    """Copy the live SQLite file into a timestamped backups/ folder.

    Uses Python's sqlite3 online backup API (via a plain connection.backup
    call) rather than shelling out to the sqlite3 CLI — it's safe to run
    while the app is serving requests (no shell=True, no string-built
    command, no dependency on the sqlite3 binary being on PATH) and works
    the same on Windows and Linux.
    """
    import sqlite3

    db_path = os.path.join(os.path.dirname(__file__), "founders_file.db")
    if not os.path.exists(db_path):
        return  # nothing to back up yet
    backup_dir = os.path.join(os.path.dirname(__file__), "backups")
    os.makedirs(backup_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = os.path.join(backup_dir, f"db_{ts}.db")

    try:
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(backup_path)
        with dest:
            source.backup(dest)
        source.close()
        dest.close()
    except Exception as exc:  # noqa: BLE001
        print(f"Database backup failed: {exc}")


def start_backup_scheduler(app):
    """Call once from app.py, inside an app context, to schedule a daily
    backup. Optional — the app runs fine without it if apscheduler isn't
    installed; this is a nice-to-have, not a hard dependency.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        app.logger.info("apscheduler not installed — skipping automatic DB backups (optional).")
        return None

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(backup_database, "cron", hour=2, minute=0, id="daily_backup", replace_existing=True)
    scheduler.start()
    return scheduler
