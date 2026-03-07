import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import current_user
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from models import db, User, OaAnnotator, OaCursor, WorkItem, Config
from auth import role_required
from s3_service import get_s3_client

oa_bp = Blueprint("oa", __name__, url_prefix="/oa")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_INSERT_BATCH = 2000


def _collect_keys_from_cursor(cursor, count, existing_keys):
    """List up to `count` new image keys from S3, advancing the cursor.

    Only does S3 API calls — no DB writes. Mutates cursor.continuation_token,
    cursor.exhausted, cursor.last_key in-place.
    """
    if cursor.exhausted or count <= 0:
        return []

    s3 = get_s3_client()
    normalized_prefix = cursor.prefix.rstrip("/") + "/" if cursor.prefix else ""
    collected = []

    while len(collected) < count:
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
            existing_keys.add(key)
            cursor.last_key = key
            collected.append(key)
            if len(collected) >= count:
                break

        if resp.get("IsTruncated"):
            cursor.continuation_token = resp["NextContinuationToken"]
        else:
            cursor.continuation_token = None
            cursor.exhausted = True
            break

    return collected


@oa_bp.route("/")
@role_required("junior_oa")
def dashboard():
    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []

    all_annotators = User.query.filter_by(role="annotator").all()
    available_annotators = [a for a in all_annotators if a.id not in annotator_ids]

    cursors = OaCursor.query.filter_by(oa_id=current_user.id).all()
    cursor = cursors  # pass list to template

    # Only count WorkItems actually assigned to an annotator (not deselected pool)
    total_distributed = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.annotator_id.isnot(None)
    ).count()
    total_annotated       = WorkItem.query.filter_by(oa_id=current_user.id, status="annotated").count()
    total_awaiting_senior = WorkItem.query.filter_by(oa_id=current_user.id, status="junior_approved").count()
    total_junior_approved = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status.in_(["junior_approved", "approved"])
    ).count()
    total_approved        = WorkItem.query.filter_by(oa_id=current_user.id, status="approved").count()
    total_rejected        = WorkItem.query.filter_by(oa_id=current_user.id, status="rejected").count()
    total_pending   = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status == "pending",
        WorkItem.annotator_id.isnot(None)
    ).count()
    unassigned = WorkItem.query.filter_by(oa_id=current_user.id, annotator_id=None).count()

    annotator_stats = []
    for ann in managed_annotators:
        assigned = WorkItem.query.filter_by(oa_id=current_user.id, annotator_id=ann.id).count()
        awaiting_junior = WorkItem.query.filter_by(
            oa_id=current_user.id, annotator_id=ann.id, status="annotated"
        ).count()
        done = WorkItem.query.filter(
            WorkItem.oa_id == current_user.id,
            WorkItem.annotator_id == ann.id,
            WorkItem.status.in_(["annotated", "junior_approved", "approved"])
        ).count()
        annotator_stats.append({"user": ann, "assigned": assigned, "awaiting_junior": awaiting_junior, "done": done})

    return render_template("oa/dashboard.html",
        managed_annotators=managed_annotators,
        available_annotators=available_annotators,
        annotator_stats=annotator_stats,
        cursors=cursors,
        total_distributed=total_distributed,
        total_annotated=total_annotated,
        total_awaiting_senior=total_awaiting_senior,
        total_junior_approved=total_junior_approved,
        total_approved=total_approved,
        total_rejected=total_rejected,
        total_pending=total_pending,
        unassigned_to_annotator=unassigned,
    )


@oa_bp.route("/add_annotator", methods=["POST"])
@role_required("junior_oa")
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
@role_required("junior_oa")
def remove_annotator(annotator_id):
    link = OaAnnotator.query.filter_by(oa_id=current_user.id, annotator_id=annotator_id).first()
    if link:
        # Return pending and rejected items to the unassigned pool.
        # Annotated/approved items keep their annotator reference (data preserved).
        WorkItem.query.filter(
            WorkItem.oa_id == current_user.id,
            WorkItem.annotator_id == annotator_id,
            WorkItem.status.in_(["pending", "rejected"])
        ).update({"annotator_id": None}, synchronize_session=False)
        db.session.delete(link)
        db.session.commit()
        flash("Annotator removed.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/distribute", methods=["POST"])
@role_required("junior_oa")
def distribute():
    """Fetch images from S3 using cursor and distribute to annotators.

    Two modes:
      equal  — distribute `per_annotator` images to ALL managed annotators
      custom — per-annotator counts via count_<id> fields
    """
    cursors = OaCursor.query.filter_by(oa_id=current_user.id, exhausted=False).all()
    has_pool = WorkItem.query.filter_by(oa_id=current_user.id, annotator_id=None, status="pending").count() > 0
    if not cursors and not has_pool:
        flash("No active S3 prefixes and no unassigned pool. Ask admin to assign prefixes.", "danger")
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
    # Track how many more each annotator still needs after pool re-assignment
    remaining = dict(distribution)

    # ── Step 1: re-assign from the unassigned pool first (fast DB ops only) ──
    for annotator_id, count in distribution.items():
        if count <= 0:
            continue
        pool_ids = [
            row[0] for row in db.session.query(WorkItem.id)
            .filter_by(oa_id=current_user.id, annotator_id=None, status="pending")
            .limit(count)
            .all()
        ]
        if pool_ids:
            reassigned = WorkItem.query.filter(
                WorkItem.id.in_(pool_ids),
                WorkItem.annotator_id.is_(None)
            ).update({"annotator_id": annotator_id}, synchronize_session=False)
            remaining[annotator_id] = count - reassigned
            total_created += reassigned

    db.session.commit()  # commit pool re-assignments

    # ── Step 2: fetch from S3 in a single combined pass for all annotators ───
    total_needed = sum(v for v in remaining.values() if v > 0)
    if total_needed > 0 and cursors:
        # Build shared existing_keys across all active cursors' prefixes (single DB query)
        prefixes = list({
            (c.prefix.rstrip("/") + "/" if c.prefix else "") for c in cursors
        })
        existing_keys = set()
        for pfx in prefixes:
            existing_keys.update(
                row[0] for row in db.session.query(WorkItem.s3_key)
                .filter(WorkItem.s3_key.like(pfx + "%"))
                .all()
            )

        # Collect all needed keys from cursors sequentially (continuation-token order)
        all_new_keys = []
        for cursor in cursors:
            if len(all_new_keys) >= total_needed or cursor.exhausted:
                continue
            keys = _collect_keys_from_cursor(
                cursor, total_needed - len(all_new_keys), existing_keys
            )
            all_new_keys.extend(keys)

        # Assign collected keys to annotators in the requested proportions
        rows_to_insert = []
        idx = 0
        for annotator_id, need in remaining.items():
            if need <= 0:
                continue
            chunk = all_new_keys[idx:idx + need]
            idx += len(chunk)
            for key in chunk:
                rows_to_insert.append({
                    "s3_key": key,
                    "filename": os.path.basename(key),
                    "oa_id": current_user.id,
                    "annotator_id": annotator_id,
                    "status": "pending",
                })

        # Single bulk insert — one commit for all annotators
        for i in range(0, len(rows_to_insert), _INSERT_BATCH):
            stmt = (
                sqlite_insert(WorkItem.__table__)
                .values(rows_to_insert[i:i + _INSERT_BATCH])
                .prefix_with("OR IGNORE")
            )
            result = db.session.execute(stmt)
            total_created += result.rowcount

        db.session.commit()  # persist inserts + all cursor states

    if total_created:
        flash(f"Distributed {total_created} images across {len(distribution)} annotators.", "success")
    else:
        flash("No new images distributed — cursor may be exhausted.", "warning")

    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/deselect/<int:annotator_id>", methods=["POST"])
@role_required("junior_oa")
def deselect_annotator(annotator_id):
    count = WorkItem.query.filter_by(
        oa_id=current_user.id, annotator_id=annotator_id, status="pending"
    ).update({"annotator_id": None})
    db.session.commit()
    flash(f"Deselected {count} pending images from annotator.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/review")
@role_required("junior_oa")
def review():
    item = WorkItem.query.filter_by(oa_id=current_user.id, status="annotated").first()
    image_id = item.id if item else None
    return render_template("oa/review.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@oa_bp.route("/review/<int:image_id>")
@role_required("junior_oa")
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
@role_required("junior_oa")
def grid():
    return render_template("oa/grid.html")


@oa_bp.route("/grid_data")
@role_required("junior_oa")
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
