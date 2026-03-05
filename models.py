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
    role = db.Column(db.String(20), nullable=False)  # 'admin', 'oa', 'annotator'
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
    oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    annotator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    status = db.Column(db.String(20), default="pending")  # pending, annotated, approved, rejected
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


class Config(db.Model):
    __tablename__ = "config"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _seed_users_from_env()


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

    valid_roles = {"admin", "oa", "annotator"}
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
