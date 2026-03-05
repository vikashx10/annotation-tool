import io
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_login import current_user
from models import db, User, OaAnnotator, ImageRecord, Assignment, Annotation, Config
from auth import role_required
from image_scanner import scan_images

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/")
@role_required("admin")
def dashboard():
    users = User.query.order_by(User.role, User.username).all()
    oas = User.query.filter_by(role="oa").all()
    annotators = User.query.filter_by(role="annotator").all()

    image_root = ""
    cfg = Config.query.get("image_root")
    if cfg:
        image_root = cfg.value

    total_images = ImageRecord.query.count()
    total_assigned = Assignment.query.count()
    total_completed = Assignment.query.filter_by(status="approved").count()
    total_under_review = Assignment.query.filter_by(status="annotated").count()

    # Per-OA stats
    oa_stats = []
    for oa in oas:
        assigned = Assignment.query.filter_by(oa_id=oa.id).count()
        completed = Assignment.query.filter_by(oa_id=oa.id, status="approved").count()
        managed = OaAnnotator.query.filter_by(oa_id=oa.id).count()
        oa_stats.append({"user": oa, "assigned": assigned, "completed": completed, "managed": managed})

    return render_template("admin/dashboard.html",
        users=users, oas=oas, annotators=annotators,
        image_root=image_root,
        total_images=total_images, total_assigned=total_assigned,
        total_completed=total_completed, total_under_review=total_under_review,
        oa_stats=oa_stats,
    )


@admin_bp.route("/create_user", methods=["POST"])
@role_required("admin")
def create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "annotator")

    if not username or not password:
        flash("Username and password are required.", "danger")
        return redirect(url_for("admin.dashboard"))

    if User.query.filter_by(username=username).first():
        flash(f"User '{username}' already exists.", "danger")
        return redirect(url_for("admin.dashboard"))

    if role not in ("admin", "oa", "annotator"):
        flash("Invalid role.", "danger")
        return redirect(url_for("admin.dashboard"))

    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f"User '{username}' created as {role}.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/delete_user/<int:user_id>", methods=["POST"])
@role_required("admin")
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Cannot delete yourself.", "danger")
        return redirect(url_for("admin.dashboard"))

    # Clean up relationships
    OaAnnotator.query.filter((OaAnnotator.oa_id == user_id) | (OaAnnotator.annotator_id == user_id)).delete()
    # Unassign their assignments
    if user.role == "annotator":
        Assignment.query.filter_by(annotator_id=user_id).update({"annotator_id": None})
    elif user.role == "oa":
        Assignment.query.filter_by(oa_id=user_id).delete()

    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.username}' deleted.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/set_image_root", methods=["POST"])
@role_required("admin")
def set_image_root():
    path = request.form.get("image_root", "").strip()
    if not path:
        flash("Path is required.", "danger")
        return redirect(url_for("admin.dashboard"))

    import os
    if not os.path.isdir(path):
        flash(f"Not a valid directory: {path}", "danger")
        return redirect(url_for("admin.dashboard"))

    cfg = Config.query.get("image_root")
    if cfg:
        cfg.value = path
    else:
        db.session.add(Config(key="image_root", value=path))
    db.session.commit()

    # Scan images
    try:
        added = scan_images(path)
        flash(f"Image root set. Scanned {added} new images (total: {ImageRecord.query.count()}).", "success")
    except Exception as e:
        flash(f"Scan error: {e}", "danger")
        return redirect(url_for("admin.dashboard"))

    # Auto-distribute to OAs
    _auto_distribute_to_oas()

    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/rescan_images", methods=["POST"])
@role_required("admin")
def rescan_images():
    cfg = Config.query.get("image_root")
    if not cfg or not cfg.value:
        flash("No image root set.", "danger")
        return redirect(url_for("admin.dashboard"))

    try:
        added = scan_images(cfg.value)
        flash(f"Rescanned. {added} new images found (total: {ImageRecord.query.count()}).", "success")
    except Exception as e:
        flash(f"Scan error: {e}", "danger")

    _auto_distribute_to_oas()
    return redirect(url_for("admin.dashboard"))


def _auto_distribute_to_oas():
    """Distribute unassigned images evenly among OAs (round-robin)."""
    oas = User.query.filter_by(role="oa").all()
    if not oas:
        return

    # Find images without assignments
    assigned_ids = db.session.query(Assignment.image_id).subquery()
    unassigned = ImageRecord.query.filter(~ImageRecord.id.in_(assigned_ids)).order_by(ImageRecord.id).all()

    if not unassigned:
        return

    for i, img in enumerate(unassigned):
        oa = oas[i % len(oas)]
        db.session.add(Assignment(image_id=img.id, oa_id=oa.id, status="pending"))

    db.session.commit()


@admin_bp.route("/user/<int:user_id>")
@role_required("admin")
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    assignments = []

    if user.role == "annotator":
        assignments = Assignment.query.filter_by(annotator_id=user_id).all()
    elif user.role == "oa":
        assignments = Assignment.query.filter_by(oa_id=user_id).all()

    total = len(assignments)
    if user.role == "annotator":
        # Annotator: completed = annotated (under review) + approved
        completed = sum(1 for a in assignments if a.status in ("annotated", "approved"))
    else:
        # OA/Admin: completed = approved only
        completed = sum(1 for a in assignments if a.status == "approved")
    approved = sum(1 for a in assignments if a.status == "approved")

    # Managed annotators (for OA)
    managed = []
    if user.role == "oa":
        links = OaAnnotator.query.filter_by(oa_id=user_id).all()
        for link in links:
            ann_user = User.query.get(link.annotator_id)
            if ann_user:
                ann_assignments = Assignment.query.filter_by(oa_id=user_id, annotator_id=ann_user.id).all()
                managed.append({
                    "user": ann_user,
                    "total": len(ann_assignments),
                    "done": sum(1 for a in ann_assignments if a.status in ("annotated", "approved")),
                })

    return render_template("admin/user_detail.html",
        user=user, assignments=assignments,
        total=total, completed=completed, approved=approved,
        managed=managed,
    )


@admin_bp.route("/export_yolo")
@role_required("admin")
def export_yolo():
    """Export approved images + YOLO label files as a zip."""
    approved = Assignment.query.filter_by(status="approved").all()
    if not approved:
        flash("No approved images to export.", "danger")
        return redirect(url_for("admin.dashboard"))

    image_root = None
    cfg = Config.query.get("image_root")
    if cfg:
        image_root = cfg.value

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for assignment in approved:
            img = ImageRecord.query.get(assignment.image_id)
            if not img:
                continue

            # Add image
            if image_root:
                import os
                full_path = os.path.join(image_root, img.relative_path)
                if os.path.exists(full_path):
                    zf.write(full_path, f"images/{img.filename}")

            # Add label
            anns = Annotation.query.filter_by(image_id=img.id).all()
            lines = []
            for a in anns:
                lines.append(f"{a.class_id} {a.x_center:.6f} {a.y_center:.6f} {a.width:.6f} {a.height:.6f}")
            label_name = os.path.splitext(img.filename)[0] + ".txt"
            zf.writestr(f"labels/{label_name}", "\n".join(lines) + ("\n" if lines else ""))

    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="yolo_export.zip")
