import os
import json
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin', 'junior_oa', 'senior_oa', 'annotator'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class OaAnnotator(db.Model):
    __tablename__ = "oa_annotators"
    id = db.Column(db.Integer, primary_key=True)
    oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    annotator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("oa_id", "annotator_id"),)

    oa = db.relationship("User", foreign_keys=[oa_id], backref="managed_annotators")
    annotator = db.relationship("User", foreign_keys=[annotator_id], backref="managed_by_oas")


class SeniorJuniorOa(db.Model):
    """Links a Senior OA to the Junior OAs they oversee."""
    __tablename__ = "senior_junior_oa"
    id = db.Column(db.Integer, primary_key=True)
    senior_oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    junior_oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("senior_oa_id", "junior_oa_id"),)

    senior = db.relationship("User", foreign_keys=[senior_oa_id])
    junior = db.relationship("User", foreign_keys=[junior_oa_id])


class OaCursor(db.Model):
    """Tracks each OA's S3 prefix and listing position.
    One OA can have multiple prefixes — each gets its own cursor row.
    """
    __tablename__ = "oa_cursors"
    id = db.Column(db.Integer, primary_key=True)
    oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bucket = db.Column(db.String(255), nullable=False)
    prefix = db.Column(db.String(500), default="")
    continuation_token = db.Column(db.Text, nullable=True)  # None = start from beginning
    exhausted = db.Column(db.Boolean, default=False)
    last_key = db.Column(db.String(500), nullable=True)  # last S3 key distributed (progress indicator)
    total_images = db.Column(db.Integer, nullable=True)   # cached S3 object count (None = not yet counted)
    __table_args__ = (db.UniqueConstraint("oa_id", "prefix", name="uq_oa_prefix"),)

    oa = db.relationship("User", foreign_keys=[oa_id])


class WorkItem(db.Model):
    """An image actively distributed for annotation.
    Created on-demand during OA distribution — never pre-populated.
    """
    __tablename__ = "work_items"
    id = db.Column(db.Integer, primary_key=True)
    s3_key = db.Column(db.String(500), unique=True, nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)   # NULL when OA deleted
    annotator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    status = db.Column(db.String(20), default="pending")  # pending, annotated, junior_approved, approved, rejected, rejected_by_senior
    reject_reason = db.Column(db.Text, nullable=True)     # comment from senior when sending back
    model_note = db.Column(db.Text, nullable=True)        # Gemini model-review verdict/notes
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    annotated_at = db.Column(db.DateTime, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    oa = db.relationship("User", foreign_keys=[oa_id])
    annotator = db.relationship("User", foreign_keys=[annotator_id])


class Annotation(db.Model):
    __tablename__ = "annotations"
    id = db.Column(db.Integer, primary_key=True)
    s3_key = db.Column(db.String(500), nullable=False, index=True)
    class_id = db.Column(db.Integer, nullable=False)
    x_center = db.Column(db.Float, nullable=False)
    y_center = db.Column(db.Float, nullable=False)
    width = db.Column(db.Float, nullable=False)
    height = db.Column(db.Float, nullable=False)


class PreAnnotation(db.Model):
    """VGT model predictions stored as pre-annotations.
    Loaded as starting annotations when annotator opens an image with no manual annotations.
    """
    __tablename__ = "pre_annotations"
    id = db.Column(db.Integer, primary_key=True)
    s3_key = db.Column(db.String(500), nullable=False, index=True)
    class_id = db.Column(db.Integer, nullable=False)
    x_center = db.Column(db.Float, nullable=False)
    y_center = db.Column(db.Float, nullable=False)
    width = db.Column(db.Float, nullable=False)
    height = db.Column(db.Float, nullable=False)
    confidence = db.Column(db.Float, nullable=True)


class Config(db.Model):
    __tablename__ = "config"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _seed_users_from_env()


def _add_missing_columns():
    """Add columns that may not exist in older databases."""
    for col in ("reject_reason", "model_note"):
        try:
            db.session.execute(db.text(
                f"ALTER TABLE work_items ADD COLUMN IF NOT EXISTS {col} TEXT"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()


def _seed_users_from_env():
    """Create users defined in USER_CONFIG env var (JSON array).

    Each entry: {"username": "...", "password": "...", "role": "admin|oa|annotator"}
    Existing usernames are skipped.
    """
    raw = os.environ.get("USER_CONFIG", "").strip()
    if not raw:
        return

    try:
        configs = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[WARNING] USER_CONFIG is not valid JSON: {e}")
        return

    if not isinstance(configs, list):
        print("[WARNING] USER_CONFIG must be a JSON array.")
        return

    valid_roles = {"admin", "junior_oa", "senior_oa", "annotator"}

    for entry in configs:
        username = entry.get("username", "").strip()
        password = entry.get("password", "").strip()
        role = entry.get("role", "").strip()

        if not username or not password or role not in valid_roles:
            print(f"[WARNING] Skipping invalid USER_CONFIG entry: {entry}")
            continue

        if User.query.filter_by(username=username).first():
            continue

        user = User(username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        print(f"[SEED] Created user '{username}' with role '{role}'")

    db.session.commit()
