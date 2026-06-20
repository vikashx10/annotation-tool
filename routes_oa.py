import os
import time
import threading
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import current_user
from sqlalchemy.dialects.postgresql import insert as pg_insert
from models import db, User, OaAnnotator, OaCursor, WorkItem, Config
from auth import role_required
from s3_service import get_s3_client

oa_bp = Blueprint("oa", __name__, url_prefix="/oa")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_INSERT_BATCH = 2000

# Background Gemini model-annotation jobs, keyed by junior OA id.
_model_annotate_jobs = {}
# How often (every N images) the worker prints a progress heartbeat.
_PRE_ANNOTATE_LOG_EVERY = int(os.environ.get("PRE_ANNOTATE_LOG_EVERY", "5") or "5")

# Junior review modes -> the WorkItem status that mode pulls from.
#   annotated          : normal review of annotator work (incl. model-annotated)
#   rejected_by_senior : fixing items the senior sent back
REVIEW_MODE_STATUS = {
    "annotated": "annotated",
    "rejected_by_senior": "rejected_by_senior",
}


def _review_target_status(review_mode):
    return REVIEW_MODE_STATUS.get(review_mode, "annotated")


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
        elif cursor.last_key:
            kwargs["StartAfter"] = cursor.last_key

        resp = s3.list_objects_v2(**kwargs)

        hit_limit = False
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
                hit_limit = True
                break

        if hit_limit:
            # Collected enough — don't mark exhausted even if this was the
            # last S3 page. Next call will resume via StartAfter=last_key.
            cursor.continuation_token = None
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
    # Of those awaiting junior review, how many were annotated by Gemini.
    total_model_annotated = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status == "annotated",
        WorkItem.model_note.isnot(None),
    ).count()
    total_awaiting_senior = WorkItem.query.filter_by(oa_id=current_user.id, status="junior_approved").count()
    total_junior_approved = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status.in_(["junior_approved", "approved"])
    ).count()
    total_approved        = WorkItem.query.filter_by(oa_id=current_user.id, status="approved").count()
    total_rejected        = WorkItem.query.filter_by(oa_id=current_user.id, status="rejected").count()
    total_rejected_by_senior = WorkItem.query.filter_by(oa_id=current_user.id, status="rejected_by_senior").count()
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
        total_model_annotated=total_model_annotated,
        total_junior_approved=total_junior_approved,
        total_approved=total_approved,
        total_rejected=total_rejected,
        total_rejected_by_senior=total_rejected_by_senior,
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
                pg_insert(WorkItem.__table__)
                .values(rows_to_insert[i:i + _INSERT_BATCH])
                .on_conflict_do_nothing(index_elements=["s3_key"])
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
    annotator_filter = request.args.get("annotator_id", type=int)
    review_mode = request.args.get("review_mode", "annotated")
    target_status = _review_target_status(review_mode)
    query = WorkItem.query.filter_by(oa_id=current_user.id, status=target_status)
    if annotator_filter:
        query = query.filter_by(annotator_id=annotator_filter)
    item = query.first()
    image_id = item.id if item else None

    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []

    return render_template("oa/review.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
        managed_annotators=managed_annotators,
        selected_annotator=annotator_filter,
        review_mode=review_mode,
    )


@oa_bp.route("/review/<int:image_id>")
@role_required("junior_oa")
def review_image(image_id):
    item = WorkItem.query.filter_by(id=image_id, oa_id=current_user.id).first()
    if not item:
        flash("Image not assigned to you.", "danger")
        return redirect(url_for("oa.dashboard"))

    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []

    review_mode = request.args.get("review_mode", "annotated")

    return render_template("oa/review.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
        managed_annotators=managed_annotators,
        selected_annotator=request.args.get("annotator_id", type=int),
        review_mode=review_mode,
    )


@oa_bp.route("/model_review", methods=["GET"])
@role_required("junior_oa")
def model_review():
    """Gemini-powered ANNOTATION over the Awaiting Junior Review pool.

    The model re-annotates images; they then go through the normal human Junior
    Review. Lets the user run a user-adjustable batch through Gemini.
    """
    from gemini_service import _get_api_key

    # Items not yet model-annotated (so a re-run doesn't redo the same images).
    awaiting = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status == "annotated",
        WorkItem.model_note.is_(None),
    ).count()
    model_annotated = WorkItem.query.filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status == "annotated",
        WorkItem.model_note.isnot(None),
    ).count()

    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []

    return render_template("oa/model_review.html",
        awaiting=awaiting,
        model_annotated=model_annotated,
        managed_annotators=managed_annotators,
        gemini_configured=bool(_get_api_key()),
    )


def _run_model_annotate(app, oa_id, item_ids, bucket):
    """Background thread: re-annotate the given WorkItems with Gemini, updating
    the job progress tracker per image (so the UI can poll a live percentage)."""
    with app.app_context():
        from gemini_service import annotate_work_item
        class_names = app.config["CLASS_NAMES"]
        job = _model_annotate_jobs[oa_id]
        tag = f"[model_annotate][OA {oa_id}]"
        log_every = max(1, _PRE_ANNOTATE_LOG_EVERY)
        total = len(item_ids)
        start_ts = time.time()
        try:
            print(f"{tag} starting — {total} image(s) to annotate", flush=True)
            for idx, item_id in enumerate(item_ids, 1):
                if job.get("stop"):
                    job["status"] = "stopped"
                    print(f"{tag} STOPPED at {idx - 1}/{total}", flush=True)
                    return

                item = WorkItem.query.get(item_id)
                if not item or item.status != "annotated":
                    job["processed"] += 1
                    continue
                try:
                    n = annotate_work_item(item, bucket, class_names)
                    if n is None:
                        db.session.rollback()
                        job["failed"] += 1
                        print(f"{tag} {idx}/{total} FAILED: {item.s3_key}", flush=True)
                    else:
                        db.session.commit()
                        job["success"] += 1
                        job["total_boxes"] += n
                except Exception as e:
                    db.session.rollback()
                    job["failed"] += 1
                    print(f"{tag} {idx}/{total} ERROR: {e}", flush=True)

                job["processed"] += 1
                if idx % log_every == 0 or idx == total:
                    elapsed = time.time() - start_ts
                    rate = idx / elapsed if elapsed > 0 else 0
                    eta = (total - idx) / rate if rate > 0 else 0
                    pct = idx * 100 // total if total else 100
                    print(f"{tag} {idx}/{total} ({pct}%) | ok={job['success']} "
                          f"failed={job['failed']} boxes={job['total_boxes']} | "
                          f"{rate:.2f} img/s, ETA {eta/60:.1f} min", flush=True)

            job["status"] = "done"
            print(f"{tag} COMPLETE — {job['success']} ok, {job['failed']} failed, "
                  f"{job['total_boxes']} boxes in {(time.time() - start_ts)/60:.1f} min", flush=True)
        except Exception as e:
            print(f"{tag} FATAL: {e}", flush=True)
            job["status"] = "error"
            job["error"] = str(e)
        finally:
            db.session.remove()


@oa_bp.route("/model_review/run", methods=["POST"])
@role_required("junior_oa")
def model_review_run():
    """Start a background Gemini annotation job over not-yet-model-annotated items."""
    from gemini_service import _get_api_key

    if not _get_api_key():
        return jsonify({"error": "Gemini not configured (set OPENROUTER_API_KEY)."}), 400

    existing = _model_annotate_jobs.get(current_user.id)
    if existing and existing["status"] == "running":
        return jsonify({"error": "Already running", "job": existing}), 409

    count = request.form.get("count", 0, type=int)
    if count <= 0:
        return jsonify({"error": "Enter how many images to send to Gemini."}), 400

    bucket = os.environ.get("AWS_S3_BUCKET", "").strip() or None
    if not bucket:
        return jsonify({"error": "S3 bucket not configured."}), 400

    annotator_filter = request.form.get("annotator_id", type=int)
    query = WorkItem.query.with_entities(WorkItem.id).filter(
        WorkItem.oa_id == current_user.id,
        WorkItem.status == "annotated",
        WorkItem.model_note.is_(None),
    )
    if annotator_filter:
        query = query.filter(WorkItem.annotator_id == annotator_filter)
    item_ids = [r[0] for r in query.limit(count).all()]

    if not item_ids:
        return jsonify({"error": "No un-annotated images left for Gemini."}), 400

    _model_annotate_jobs[current_user.id] = {
        "status": "running",
        "processed": 0,
        "total": len(item_ids),
        "success": 0,
        "failed": 0,
        "total_boxes": 0,
    }

    app = current_app._get_current_object()
    t = threading.Thread(target=_run_model_annotate,
                         args=(app, current_user.id, item_ids, bucket), daemon=True)
    t.start()
    return jsonify({"status": "started", "total": len(item_ids)})


@oa_bp.route("/model_review/status")
@role_required("junior_oa")
def model_review_status():
    """Poll endpoint for the Gemini annotation job progress."""
    job = _model_annotate_jobs.get(current_user.id)
    if not job:
        return jsonify({"status": "idle"})
    return jsonify(job)


@oa_bp.route("/model_review/stop", methods=["POST"])
@role_required("junior_oa")
def model_review_stop():
    """Signal the running Gemini annotation job to stop."""
    job = _model_annotate_jobs.get(current_user.id)
    if not job or job["status"] != "running":
        return jsonify({"error": "No running job to stop."}), 400
    job["stop"] = True
    return jsonify({"status": "stopping"})


@oa_bp.route("/grid")
@role_required("junior_oa")
def grid():
    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []
    return render_template("oa/grid.html", managed_annotators=managed_annotators)


@oa_bp.route("/grid_data")
@role_required("junior_oa")
def grid_data():
    page = request.args.get("page", 1, type=int)
    status_filter = request.args.get("status", "")
    annotator_filter = request.args.get("annotator_id", "", type=str)
    PAGE_SIZE = 100

    query = WorkItem.query.filter_by(oa_id=current_user.id)
    if annotator_filter:
        query = query.filter_by(annotator_id=int(annotator_filter))
    if status_filter:
        query = query.filter_by(status=status_filter)

    base = WorkItem.query.filter_by(oa_id=current_user.id)
    if annotator_filter:
        base = base.filter_by(annotator_id=int(annotator_filter))
    total = base.count()
    to_review = base.filter_by(status="annotated").count()
    approved = base.filter_by(status="approved").count()

    paginated = query.order_by(WorkItem.status.desc(), WorkItem.id)\
        .paginate(page=page, per_page=PAGE_SIZE, error_out=False)

    images = [
        {
            "id": w.id, "filename": w.filename, "status": w.status,
            "annotator_id": w.annotator_id,
            "annotator": w.annotator.username if w.annotator else None,
            "annotated_at": w.annotated_at.isoformat() if w.annotated_at else None,
            "reviewed_at": w.reviewed_at.isoformat() if w.reviewed_at else None,
        }
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
