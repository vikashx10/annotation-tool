import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import current_user
from models import db, User, OaAnnotator, OaCursor, WorkItem, Config
from auth import role_required
from s3_service import get_s3_client

oa_bp = Blueprint("oa", __name__, url_prefix="/oa")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DISTRIBUTE_BATCH = 500


def _fetch_and_create_work_items(cursor, annotator_id, count):
    """Advance the OA's S3 cursor by `count` images, create WorkItems in batches.

    Returns the number of WorkItems actually created.
    """
    if cursor.exhausted or count <= 0:
        return 0

    s3 = get_s3_client()
    normalized_prefix = cursor.prefix.rstrip("/") + "/" if cursor.prefix else ""

    # Load already-distributed keys to avoid duplicates
    existing_keys = set(
        row[0] for row in db.session.query(WorkItem.s3_key).all()
    )

    collected = []
    exhausted = False

    while len(collected) < count and not exhausted:
        kwargs = {
            "Bucket": cursor.bucket,
            "Prefix": normalized_prefix,
            "MaxKeys": 1000,
        }
        if cursor.continuation_token:
            kwargs["ContinuationToken"] = cursor.continuation_token

        resp = s3.list_objects_v2(**kwargs)

        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if os.path.splitext(key)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            if key in existing_keys:
                continue
            existing_keys.add(key)  # prevent in-flight duplicates
            cursor.last_key = key   # track progress
            collected.append({
                "s3_key": key,
                "filename": os.path.basename(key),
                "oa_id": cursor.oa_id,
                "annotator_id": annotator_id,
                "status": "pending",
            })
            if len(collected) >= count:
                break

        if resp.get("IsTruncated"):
            cursor.continuation_token = resp["NextContinuationToken"]
        else:
            cursor.continuation_token = None
            exhausted = True

    cursor.exhausted = exhausted

    # Batch insert in chunks of DISTRIBUTE_BATCH
    total_created = 0
    for i in range(0, len(collected), DISTRIBUTE_BATCH):
        chunk = collected[i:i + DISTRIBUTE_BATCH]
        db.session.bulk_insert_mappings(WorkItem, chunk)
        db.session.commit()
        total_created += len(chunk)

    return total_created


@oa_bp.route("/")
@role_required("oa")
def dashboard():
    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []

    all_annotators = User.query.filter_by(role="annotator").all()
    available_annotators = [a for a in all_annotators if a.id not in annotator_ids]

    cursors = OaCursor.query.filter_by(oa_id=current_user.id).all()
    cursor = cursors  # pass list to template

    total_distributed = WorkItem.query.filter_by(oa_id=current_user.id).count()
    total_annotated = WorkItem.query.filter_by(oa_id=current_user.id, status="annotated").count()
    total_approved = WorkItem.query.filter_by(oa_id=current_user.id, status="approved").count()
    total_rejected = WorkItem.query.filter_by(oa_id=current_user.id, status="rejected").count()
    total_pending = WorkItem.query.filter_by(oa_id=current_user.id, status="pending").count()
    unassigned = WorkItem.query.filter_by(oa_id=current_user.id, annotator_id=None).count()

    annotator_stats = []
    for ann in managed_annotators:
        assigned = WorkItem.query.filter_by(oa_id=current_user.id, annotator_id=ann.id).count()
        done = WorkItem.query.filter_by(oa_id=current_user.id, annotator_id=ann.id)\
            .filter(WorkItem.status.in_(["annotated", "approved"])).count()
        annotator_stats.append({"user": ann, "assigned": assigned, "done": done})

    return render_template("oa/dashboard.html",
        managed_annotators=managed_annotators,
        available_annotators=available_annotators,
        annotator_stats=annotator_stats,
        cursors=cursors,
        total_distributed=total_distributed,
        total_annotated=total_annotated,
        total_approved=total_approved,
        total_rejected=total_rejected,
        total_pending=total_pending,
        unassigned_to_annotator=unassigned,
    )


@oa_bp.route("/add_annotator", methods=["POST"])
@role_required("oa")
def add_annotator():
    annotator_id = request.form.get("annotator_id", type=int)
    if not annotator_id:
        flash("Select an annotator.", "danger")
        return redirect(url_for("oa.dashboard"))

    user = User.query.get(annotator_id)
    if not user or user.role != "annotator":
        flash("Invalid annotator.", "danger")
        return redirect(url_for("oa.dashboard"))

    existing = OaAnnotator.query.filter_by(oa_id=current_user.id, annotator_id=annotator_id).first()
    if existing:
        flash("Already managing this annotator.", "danger")
        return redirect(url_for("oa.dashboard"))

    db.session.add(OaAnnotator(oa_id=current_user.id, annotator_id=annotator_id))
    db.session.commit()
    flash(f"Added annotator '{user.username}'.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/remove_annotator/<int:annotator_id>", methods=["POST"])
@role_required("oa")
def remove_annotator(annotator_id):
    link = OaAnnotator.query.filter_by(oa_id=current_user.id, annotator_id=annotator_id).first()
    if link:
        WorkItem.query.filter_by(
            oa_id=current_user.id, annotator_id=annotator_id, status="pending"
        ).update({"annotator_id": None})
        db.session.delete(link)
        db.session.commit()
        flash("Annotator removed.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/distribute", methods=["POST"])
@role_required("oa")
def distribute():
    """Fetch images from S3 using cursor and distribute to annotators.

    Two modes:
      equal  — distribute `per_annotator` images to ALL managed annotators
      custom — per-annotator counts via count_<id> fields
    """
    cursors = OaCursor.query.filter_by(oa_id=current_user.id, exhausted=False).all()
    if not cursors:
        flash("No active S3 prefixes. Ask admin to assign prefixes.", "danger")
        return redirect(url_for("oa.dashboard"))

    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    if not links:
        flash("Add annotators first.", "danger")
        return redirect(url_for("oa.dashboard"))

    mode = request.form.get("mode", "custom")
    distribution = {}  # annotator_id -> count

    if mode == "equal":
        per_annotator = request.form.get("per_annotator", 0, type=int)
        if per_annotator <= 0:
            flash("Enter a valid number of images per annotator.", "danger")
            return redirect(url_for("oa.dashboard"))
        for link in links:
            distribution[link.annotator_id] = per_annotator
    else:
        for link in links:
            count = request.form.get(f"count_{link.annotator_id}", 0, type=int)
            if count > 0:
                distribution[link.annotator_id] = count

    if not distribution:
        flash("No counts specified.", "warning")
        return redirect(url_for("oa.dashboard"))

    total_created = 0
    for annotator_id, count in distribution.items():
        remaining = count
        # Draw from each active cursor in order until count is fulfilled
        for cursor in cursors:
            if remaining <= 0 or cursor.exhausted:
                continue
            created = _fetch_and_create_work_items(cursor, annotator_id, remaining)
            remaining -= created
            total_created += created

    db.session.commit()  # persist all cursor states

    if total_created:
        flash(f"Distributed {total_created} images across {len(distribution)} annotators.", "success")
    else:
        flash("No new images distributed — cursor may be exhausted.", "warning")

    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/deselect/<int:annotator_id>", methods=["POST"])
@role_required("oa")
def deselect_annotator(annotator_id):
    count = WorkItem.query.filter_by(
        oa_id=current_user.id, annotator_id=annotator_id, status="pending"
    ).update({"annotator_id": None})
    db.session.commit()
    flash(f"Deselected {count} pending images from annotator.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/review")
@role_required("oa")
def review():
    item = WorkItem.query.filter_by(oa_id=current_user.id, status="annotated").first()
    image_id = item.id if item else None
    return render_template("oa/review.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@oa_bp.route("/review/<int:image_id>")
@role_required("oa")
def review_image(image_id):
    item = WorkItem.query.filter_by(id=image_id, oa_id=current_user.id).first()
    if not item:
        flash("Image not assigned to you.", "danger")
        return redirect(url_for("oa.dashboard"))
    return render_template("oa/review.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@oa_bp.route("/grid")
@role_required("oa")
def grid():
    return render_template("oa/grid.html")


@oa_bp.route("/grid_data")
@role_required("oa")
def grid_data():
    page = request.args.get("page", 1, type=int)
    status_filter = request.args.get("status", "")
    PAGE_SIZE = 100

    query = WorkItem.query.filter_by(oa_id=current_user.id)
    if status_filter:
        query = query.filter_by(status=status_filter)

    base = WorkItem.query.filter_by(oa_id=current_user.id)
    total = base.count()
    to_review = base.filter_by(status="annotated").count()
    approved = base.filter_by(status="approved").count()

    paginated = query.order_by(WorkItem.status.desc(), WorkItem.id)\
        .paginate(page=page, per_page=PAGE_SIZE, error_out=False)

    images = [
        {"id": w.id, "filename": w.filename, "status": w.status, "annotator_id": w.annotator_id}
        for w in paginated.items
    ]

    return jsonify({
        "images": images,
        "total": total,
        "to_review": to_review,
        "approved": approved,
        "page": page,
        "pages": paginated.pages,
        "has_next": paginated.has_next,
    })
