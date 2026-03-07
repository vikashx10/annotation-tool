import os
from pathlib import Path
from flask import Flask, redirect, url_for
from flask_login import current_user
from sqlalchemy import event
from models import db, init_db
from auth import auth_bp, login_manager

# Layout classes (12 total)
CLASS_NAMES = [
    "Header", "Footer", "Title", "Text", "Table", "Figure",
    "Caption", "Equation", "List Item", "Page Number",
    "Section Header", "Key-Value Pair", "Signature", "Seal",
]

BASE_DIR = Path(__file__).parent


def _migrate_db(app):
    """Add columns / schema changes that may not exist in older DB schemas."""
    with app.app_context():
        from sqlalchemy import text
        with db.engine.connect() as conn:
            # Add total_images to oa_cursors if missing
            for col, definition in [
                ("total_images", "INTEGER"),
            ]:
                try:
                    conn.execute(text(f"ALTER TABLE oa_cursors ADD COLUMN {col} {definition}"))
                    conn.commit()
                except Exception:
                    pass  # column already exists

            # Make work_items.oa_id nullable so WorkItems survive OA deletion
            try:
                result = conn.execute(text("PRAGMA table_info(work_items)"))
                cols = {row[1]: row[3] for row in result.fetchall()}  # name -> notnull
                if cols.get("oa_id") == 1:  # 1 = NOT NULL — needs migration
                    conn.execute(text("DROP TABLE IF EXISTS _work_items_v2"))
                    conn.execute(text("""
                        CREATE TABLE _work_items_v2 (
                            id       INTEGER PRIMARY KEY,
                            s3_key   VARCHAR(500) NOT NULL,
                            filename VARCHAR(255) NOT NULL,
                            oa_id    INTEGER REFERENCES users(id),
                            annotator_id INTEGER REFERENCES users(id),
                            status   VARCHAR(20) DEFAULT 'pending',
                            created_at   DATETIME,
                            annotated_at DATETIME,
                            reviewed_at  DATETIME
                        )
                    """))
                    conn.execute(text("INSERT INTO _work_items_v2 SELECT * FROM work_items"))
                    conn.execute(text("DROP TABLE work_items"))
                    conn.execute(text("ALTER TABLE _work_items_v2 RENAME TO work_items"))
                    conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_work_items_s3_key ON work_items (s3_key)"
                    ))
                    conn.commit()
                    print("[migrate] work_items.oa_id is now nullable")
            except Exception as e:
                print(f"[migrate] work_items.oa_id migration failed: {e}")
                try:
                    conn.execute(text("DROP TABLE IF EXISTS _work_items_v2"))
                    conn.commit()
                except Exception:
                    pass

            # Rename legacy 'oa' role to 'junior_oa'
            try:
                conn.execute(text("UPDATE users SET role='junior_oa' WHERE role='oa'"))
                conn.commit()
            except Exception:
                pass


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{BASE_DIR / 'instance' / 'annotations.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False},
        "pool_timeout": 20,
    }
    app.config["CLASS_NAMES"] = CLASS_NAMES

    # Ensure instance dir exists
    (BASE_DIR / "instance").mkdir(exist_ok=True)

    # Init extensions
    init_db(app)

    # Enable WAL mode for concurrent thread access
    with app.app_context():
        @event.listens_for(db.engine, "connect")
        def set_wal_mode(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA busy_timeout=10000")

    # Run any pending column migrations
    _migrate_db(app)
    login_manager.init_app(app)

    # Register blueprints
    app.register_blueprint(auth_bp)

    from routes_api import api_bp
    app.register_blueprint(api_bp)

    from routes_admin import admin_bp
    app.register_blueprint(admin_bp)

    from routes_annotator import annotator_bp
    app.register_blueprint(annotator_bp)

    from routes_oa import oa_bp
    app.register_blueprint(oa_bp)

    from routes_senior_oa import senior_oa_bp
    app.register_blueprint(senior_oa_bp)

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            from auth import _redirect_by_role
            return _redirect_by_role(current_user.role)
        return redirect(url_for("auth.login"))

    return app


if __name__ == "__main__":
    app = create_app()
    print("Starting server at http://localhost:8000")
    app.run(debug=True, port=8000, host="0.0.0.0")
