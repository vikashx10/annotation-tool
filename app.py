import os
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, redirect, url_for
from flask_login import current_user
from models import db, init_db
from auth import auth_bp, login_manager

load_dotenv()

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
                conn.execute(text(
                    "ALTER TABLE work_items ALTER COLUMN oa_id DROP NOT NULL"
                ))
                conn.commit()
                print("[migrate] work_items.oa_id is now nullable")
            except Exception:
                pass  # already nullable or table doesn't exist yet

            # Rename legacy 'oa' role to 'junior_oa'
            try:
                conn.execute(text("UPDATE users SET role='junior_oa' WHERE role='oa'"))
                conn.commit()
            except Exception:
                pass

        # users.senior_oa_id is added in models._add_missing_columns(), which runs
        # before any ORM query. Only the data backfill belongs here.
        _backfill_junior_oa_owner()


# Senior OA that pre-existing Junior OAs are handed to when the ownership
# column is first introduced. Override per-environment if needed.
BACKFILL_SENIOR_USERNAME = os.environ.get(
    "SENIOR_OA_BACKFILL_USERNAME", "demo_senior_oa"
).strip()

# Config key marking the Junior OA backfill as already applied.
_JUNIOR_BACKFILL_FLAG = "migration:junior_oa_owner_backfill"


def _backfill_junior_oa_owner():
    """One-time: hand every pre-existing Junior OA to BACKFILL_SENIOR_USERNAME.

    Also clears any annotator ownership left over from the earlier revision of
    this feature, when senior_oa_id briefly scoped annotators instead. Junior
    OA pickers no longer consult it, so a stale owner there would only mislead.

    Runs at most once per database — guarded by a row in `config` — so Junior
    OAs created later are NOT swept into the same senior on the next restart.
    """
    from models import Config, User

    if Config.query.get(_JUNIOR_BACKFILL_FLAG):
        return

    senior = User.query.filter_by(
        username=BACKFILL_SENIOR_USERNAME, role="senior_oa"
    ).first()
    if not senior:
        # Senior does not exist yet (fresh DB, or seeded later). Leave the flag
        # unset so the backfill still runs once that account shows up.
        print(f"[migrate] senior OA '{BACKFILL_SENIOR_USERNAME}' not found — "
              "Junior OA backfill deferred")
        return

    cleared = User.query.filter(
        User.role == "annotator",
        User.senior_oa_id.isnot(None),
    ).update({"senior_oa_id": None}, synchronize_session=False)

    updated = User.query.filter(
        User.role == "junior_oa",
        User.senior_oa_id.is_(None),
    ).update({"senior_oa_id": senior.id}, synchronize_session=False)

    db.session.add(Config(key=_JUNIOR_BACKFILL_FLAG, value=str(senior.id)))
    db.session.commit()
    print(f"[migrate] assigned {updated} existing Junior OA(s) to "
          f"'{BACKFILL_SENIOR_USERNAME}'")
    if cleared:
        print(f"[migrate] cleared stale annotator ownership on {cleared} row(s)")


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # SQLite fallback: ensure instance/ directory exists
    instance_dir = BASE_DIR / "instance"
    instance_dir.mkdir(exist_ok=True)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", f"sqlite:///{instance_dir / 'annotations.db'}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_timeout": 20,
        "pool_pre_ping": True,
    }
    app.config["CLASS_NAMES"] = CLASS_NAMES

    # Init extensions
    init_db(app)

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
