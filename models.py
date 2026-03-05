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


class ImageRecord(db.Model):
    __tablename__ = "images"
    id = db.Column(db.Integer, primary_key=True)
    relative_path = db.Column(db.String(500), unique=True, nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Assignment(db.Model):
    __tablename__ = "assignments"
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey("images.id"), unique=True, nullable=False)
    oa_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    annotator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    status = db.Column(db.String(20), default="pending")  # pending, annotated, approved, rejected
    assigned_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    annotated_at = db.Column(db.DateTime, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    image = db.relationship("ImageRecord", backref="assignment", uselist=False)
    oa = db.relationship("User", foreign_keys=[oa_id])
    annotator = db.relationship("User", foreign_keys=[annotator_id])


class Annotation(db.Model):
    __tablename__ = "annotations"
    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(db.Integer, db.ForeignKey("images.id"), nullable=False)
    class_id = db.Column(db.Integer, nullable=False)
    x_center = db.Column(db.Float, nullable=False)
    y_center = db.Column(db.Float, nullable=False)
    width = db.Column(db.Float, nullable=False)
    height = db.Column(db.Float, nullable=False)

    image = db.relationship("ImageRecord", backref="annotations")


class Config(db.Model):
    __tablename__ = "config"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        # Seed default users if no users exist
        if User.query.count() == 0:
            admin = User(username="VioletSkeleton", role="admin")
            admin.set_password("Welcome@123")
            db.session.add(admin)

            oa = User(username="UnsiloedOA1", role="oa")
            oa.set_password("Hello123")
            db.session.add(oa)

            db.session.commit()
            print("Default admin created: VioletSkeleton / Welcome@123")
            print("Default OA created: UnsiloedOA1 / Hello123")
