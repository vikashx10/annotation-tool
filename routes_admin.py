import io
import os
import threading
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app
from flask_login import current_user
from models import db, User, OaAnnotator, OaCursor, WorkItem, Annotation
from auth import role_required
from s3_service import validate_bucket_access, get_object_bytes, count_s3_images

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _start_count_thread(app, cursor_id, bucket, prefix):
    """Count S3 images in background and store result in OaCursor.total_images."""
    def _run():
        with app.app_context():
            try:
                total = count_s3_images(bucket, prefix)
                cursor = OaCursor.query.get(cursor_id)
                if cursor:
                    cursor.total_images = total
                    db.session.commit()
            except Exception as e:
                print(f"[count_thread] Error counting {prefix}: {e}")
                db.session.rollback()
            finally:
                db.session.remove()  # return connection to pool cleanly
    t = threading.Thread(target=_run, daemon=True)
    t.start()


@admin_bp.route("/")
@role_required("admin")
def dashboard():
    users = User.query.order_by(User.role, User.username).all()
    oas = User.query.filter_by(role="oa").all()

    s3_bucket = os.environ.get("AWS_S3_BUCKET", "").strip()

    total_distributed = WorkItem.query.count()
    total_assigned = WorkItem.query.filter(WorkItem.annotator_id.isnot(None)).count()
    total_completed = WorkItem.query.filter_by(status="approved").count()
    total_under_review = WorkItem.query.filter_by(status="annotated").count()

    oa_stats = []
    for oa in oas:
        cursor = OaCursor.query.filter_by(oa_id=oa.id).all()
        base = WorkItem.query.filter_by(oa_id=oa.id)
        distributed = base.count()
        pending    = base.filter_by(status="pending").count()
        annotated  = WorkItem.query.filter_by(oa_id=oa.id, status="annotated").count()
        approved   = WorkItem.query.filter_by(oa_id=oa.id, status="approved").count()
        rejected   = WorkItem.query.filter_by(oa_id=oa.id, status="rejected").count()
        managed    = OaAnnotator.query.filter_by(oa_id=oa.id).count()
        oa_stats.append({
            "user": oa,
            "cursor": cursor,
            "distributed": distributed,
            "pending": pending,
            "annotated": annotated,
            "approved": approved,
            "rejected": rejected,
            "managed": managed,
        })

    # Build a set of already-assigned prefixes for the UI to mark as taken
    assigned_prefixes = {
        c.prefix: User.query.get(c.oa_id).username
        for c in OaCursor.query.all()
        if User.query.get(c.oa_id)
    }

    return render_template("admin/dashboard.html",
        users=users, oas=oas,
        s3_bucket=s3_bucket,
        assigned_prefixes=assigned_prefixes,
        total_distributed=total_distributed,
        total_assigned=total_assigned,
        total_completed=total_completed,
        total_under_review=total_under_review,
        oa_stats=oa_stats,
    )



@admin_bp.route("/assign_oa_prefix", methods=["POST"])
@role_required("admin")
def assign_oa_prefix():
    """Assign one or more S3 prefixes to an OA. Each prefix gets its own cursor."""
    oa_id = request.form.get("oa_id", type=int)
    prefixes = [p.strip() for p in request.form.getlist("prefix") if p.strip()]

    if not prefixes:
        flash("Select at least one S3 prefix.", "danger")
        return redirect(url_for("admin.dashboard"))

    bucket = os.environ.get("AWS_S3_BUCKET", "").strip()
    if not bucket:
        flash("AWS_S3_BUCKET is not set in environment.", "danger")
        return redirect(url_for("admin.dashboard"))
    oa = User.query.get_or_404(oa_id)
    if oa.role != "oa":
        flash("Selected user is not an OA.", "danger")
        return redirect(url_for("admin.dashboard"))

    added, skipped = 0, 0
    new_cursors = []  # collect to start threads after commit
    for prefix in prefixes:
        try:
            validate_bucket_access(bucket, prefix)
        except ValueError as e:
            flash(f"Skipping '{prefix}': {e}", "warning")
            skipped += 1
            continue

        # Block if this prefix is already assigned to ANY OA
        existing = OaCursor.query.filter_by(prefix=prefix).first()
        if existing:
            owner = User.query.get(existing.oa_id)
            owner_name = owner.username if owner else f"OA #{existing.oa_id}"
            if existing.oa_id == oa_id:
                flash(f"'{prefix}' is already assigned to {owner_name}.", "warning")
            else:
                flash(f"'{prefix}' is already assigned to {owner_name} — cannot assign to multiple OAs.", "danger")
            skipped += 1
            continue

        cursor = OaCursor(
            oa_id=oa_id, bucket=bucket, prefix=prefix,
            continuation_token=None, exhausted=False,
            total_images=None,  # filled by background thread after commit
        )
        db.session.add(cursor)
        new_cursors.append((cursor, bucket, prefix))
        added += 1

    db.session.commit()  # commit first so cursor IDs exist in DB

    # Start background count threads AFTER commit
    app = current_app._get_current_object()
    for cursor, b, p in new_cursors:
        _start_count_thread(app, cursor.id, b, p)
    flash(f"Assigned {added} new prefix(es) to '{oa.username}'" + (f" ({skipped} skipped/duplicate)" if skipped else "") + ".", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/remove_oa_prefix/<int:cursor_id>", methods=["POST"])
@role_required("admin")
def remove_oa_prefix(cursor_id):
    cursor = OaCursor.query.get_or_404(cursor_id)
    db.session.delete(cursor)
    db.session.commit()
    flash("Prefix removed.", "success")
    return redirect(url_for("admin.dashboard"))


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

    if user.role == "annotator":
        # Return ALL of this annotator's work to the OA's unassigned pool.
        # Annotated items stay reviewable by OA; approved items keep their data.
        WorkItem.query.filter_by(annotator_id=user_id).update(
            {"annotator_id": None}, synchronize_session=False
        )
        OaAnnotator.query.filter_by(annotator_id=user_id).delete()

    elif user.role == "oa":
        # Preserve annotated/approved items — null oa_id so they remain for export.
        WorkItem.query.filter(
            WorkItem.oa_id == user_id,
            WorkItem.status.in_(["annotated", "approved"])
        ).update({"oa_id": None, "annotator_id": None}, synchronize_session=False)

        # Delete un-annotated items — nothing valuable to keep.
        WorkItem.query.filter(
            WorkItem.oa_id == user_id,
            WorkItem.status.in_(["pending", "rejected"])
        ).delete(synchronize_session=False)

        # Free the S3 prefixes so admin can reassign them.
        OaCursor.query.filter_by(oa_id=user_id).delete()
        OaAnnotator.query.filter(
            (OaAnnotator.oa_id == user_id) | (OaAnnotator.annotator_id == user_id)
        ).delete()

    else:  # admin — no work items owned, just clean up any OA links
        OaAnnotator.query.filter(
            (OaAnnotator.oa_id == user_id) | (OaAnnotator.annotator_id == user_id)
        ).delete()

    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.username}' deleted.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/user/<int:user_id>")
@role_required("admin")
def user_detail(user_id):
    user = User.query.get_or_404(user_id)
    work_items = []

    if user.role == "annotator":
        work_items = WorkItem.query.filter_by(annotator_id=user_id).all()
    elif user.role == "oa":
        work_items = WorkItem.query.filter_by(oa_id=user_id).all()

    total = len(work_items)
    if user.role == "annotator":
        completed = sum(1 for w in work_items if w.status in ("annotated", "approved"))
    else:
        completed = sum(1 for w in work_items if w.status == "approved")
    approved = sum(1 for w in work_items if w.status == "approved")

    managed = []
    if user.role == "oa":
        links = OaAnnotator.query.filter_by(oa_id=user_id).all()
        for link in links:
            ann_user = User.query.get(link.annotator_id)
            if ann_user:
                ann_items = WorkItem.query.filter_by(oa_id=user_id, annotator_id=ann_user.id).all()
                managed.append({
                    "user": ann_user,
                    "total": len(ann_items),
                    "done": sum(1 for w in ann_items if w.status in ("annotated", "approved")),
                })

    cursor = OaCursor.query.filter_by(oa_id=user_id).all() if user.role == "oa" else []

    return render_template("admin/user_detail.html",
        user=user, assignments=work_items,
        total=total, completed=completed, approved=approved,
        managed=managed, cursor=cursor,
    )


@admin_bp.route("/export_yolo")
@role_required("admin")
def export_yolo():
    """Export approved images + YOLO label files as a zip (streamed from S3)."""
    approved = WorkItem.query.filter_by(status="approved").all()
    if not approved:
        flash("No approved images to export.", "danger")
        return redirect(url_for("admin.dashboard"))

    bucket = os.environ.get("AWS_S3_BUCKET", "").strip() or None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in approved:
            if bucket:
                try:
                    image_bytes = get_object_bytes(bucket, item.s3_key)
                    zf.writestr(item.s3_key, image_bytes)  # preserve full path
                except Exception:
                    pass

            anns = Annotation.query.filter_by(s3_key=item.s3_key).all()
            lines = [
                f"{a.class_id} {a.x_center:.6f} {a.y_center:.6f} {a.width:.6f} {a.height:.6f}"
                for a in anns
            ]
            label_name = os.path.splitext(item.filename)[0] + ".txt"
            # Mirror S3 structure: same relative folder + annotated/ subfolder
            rel_folder = os.path.dirname(item.s3_key)
            zip_label_path = f"{rel_folder}/annotated/{label_name}" if rel_folder else f"annotated/{label_name}"
            zip_image_path = item.s3_key  # keep original path for images
            zf.writestr(zip_label_path, "\n".join(lines) + ("\n" if lines else ""))

    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="yolo_export.zip")
