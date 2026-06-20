import io
import os
import time
import threading
import zipfile
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app, jsonify
from flask_login import current_user
from models import db, User, OaAnnotator, OaCursor, WorkItem, Annotation, SeniorJuniorOa, PreAnnotation
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
    oas = User.query.filter(User.role.in_(["junior_oa", "senior_oa"])).order_by(User.role, User.username).all()

    s3_bucket = os.environ.get("AWS_S3_BUCKET", "").strip()

    total_distributed = WorkItem.query.count()
    total_assigned = WorkItem.query.filter(WorkItem.annotator_id.isnot(None)).count()
    total_completed = WorkItem.query.filter_by(status="approved").count()
    total_under_review = WorkItem.query.filter_by(status="annotated").count()
    total_junior_approved = WorkItem.query.filter_by(status="junior_approved").count()

    oa_stats = []
    for oa in oas:
        if oa.role != "junior_oa":
            continue
        cursor = OaCursor.query.filter_by(oa_id=oa.id).all()
        base = WorkItem.query.filter_by(oa_id=oa.id)
        distributed     = base.count()
        pending         = WorkItem.query.filter_by(oa_id=oa.id, status="pending").count()
        annotated       = WorkItem.query.filter_by(oa_id=oa.id, status="annotated").count()
        junior_approved = WorkItem.query.filter_by(oa_id=oa.id, status="junior_approved").count()
        approved        = WorkItem.query.filter_by(oa_id=oa.id, status="approved").count()
        managed         = OaAnnotator.query.filter_by(oa_id=oa.id).count()

        # Count how many S3 keys under this OA's prefixes already have pre-annotations
        pre_annotated = 0
        total_cursor_images = 0
        for c in cursor:
            if c.total_images:
                total_cursor_images += c.total_images
            if c.prefix:
                cnt = db.session.query(db.func.count(db.distinct(PreAnnotation.s3_key))).filter(
                    PreAnnotation.s3_key.like(f"{c.prefix}%")
                ).scalar() or 0
                pre_annotated += cnt

        oa_stats.append({
            "user": oa,
            "cursor": cursor,
            "distributed": distributed,
            "pending": pending,
            "annotated": annotated,
            "junior_approved": junior_approved,
            "approved": approved,
            "managed": managed,
            "pre_annotated": pre_annotated,
            "total_cursor_images": total_cursor_images,
        })

    # Build a set of already-assigned prefixes for the UI to mark as taken
    assigned_prefixes = {
        c.prefix: User.query.get(c.oa_id).username
        for c in OaCursor.query.all()
        if User.query.get(c.oa_id)
    }

    junior_oas = [o for o in oas if o.role == "junior_oa"]

    return render_template("admin/dashboard.html",
        users=users, oas=oas, junior_oas=junior_oas,
        s3_bucket=s3_bucket,
        assigned_prefixes=assigned_prefixes,
        total_distributed=total_distributed,
        total_assigned=total_assigned,
        total_completed=total_completed,
        total_under_review=total_under_review,
        total_junior_approved=total_junior_approved,
        oa_stats=oa_stats,
        pre_annotate_jobs=_pre_annotate_jobs,
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
    if oa.role != "junior_oa":
        flash("S3 prefixes can only be assigned to Junior OAs.", "danger")
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

    if role not in ("admin", "junior_oa", "senior_oa", "annotator"):
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

    elif user.role == "junior_oa":
        # Preserve annotated/junior_approved/approved items — null oa_id for export.
        WorkItem.query.filter(
            WorkItem.oa_id == user_id,
            WorkItem.status.in_(["annotated", "junior_approved", "approved"])
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
        SeniorJuniorOa.query.filter_by(junior_oa_id=user_id).delete()

    elif user.role == "senior_oa":
        # Just remove their senior links — junior OAs and their work are unaffected.
        SeniorJuniorOa.query.filter_by(senior_oa_id=user_id).delete()

    else:  # admin — no work items owned, just clean up any OA links
        OaAnnotator.query.filter(
            (OaAnnotator.oa_id == user_id) | (OaAnnotator.annotator_id == user_id)
        ).delete()
        SeniorJuniorOa.query.filter(
            (SeniorJuniorOa.senior_oa_id == user_id) | (SeniorJuniorOa.junior_oa_id == user_id)
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
    elif user.role == "junior_oa":
        work_items = WorkItem.query.filter_by(oa_id=user_id).all()

    total = len(work_items)
    if user.role == "annotator":
        completed = sum(1 for w in work_items if w.status in ("annotated", "approved"))
    else:
        completed = sum(1 for w in work_items if w.status == "approved")
    approved = sum(1 for w in work_items if w.status == "approved")

    managed = []
    if user.role == "junior_oa":
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

    cursor = OaCursor.query.filter_by(oa_id=user_id).all() if user.role == "junior_oa" else []

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


# ── Background pre-annotation jobs ──────────────────────────────────────
# In-memory progress tracker. Keyed by oa_id.
# Each entry: {"status": "running"|"done"|"error", "processed": N, "total": N,
#              "success": N, "failed": N, "total_boxes": N}
_pre_annotate_jobs = {}

# How often (every N images) to print a progress heartbeat line.
_PRE_ANNOTATE_LOG_EVERY = int(os.environ.get("PRE_ANNOTATE_LOG_EVERY", "5") or "5")


def _detect_pre_annotation_boxes(model, img_bytes, filename, class_names):
    """Run the chosen model on one image.

    Returns (responded, boxes): `responded` is True if the model returned a
    (possibly empty) result; boxes is the detected box list.
    """
    if model == "gemini":
        from gemini_service import reannotate_image
        g_boxes = reannotate_image(img_bytes, filename, class_names)
        if g_boxes is None:
            return False, []
        return True, [{**b, "confidence": 1.0} for b in g_boxes]

    # Default: VGT
    from vgt_service import call_vgt_api, convert_detections
    detections = call_vgt_api(img_bytes, filename=filename)
    if detections is None:
        return False, []
    return True, convert_detections(detections)


def _run_pre_annotate(app, oa_id, bucket, prefixes, model="vgt"):
    """Background thread: run the chosen model (VGT, Gemini, or both) on all
    S3 images in the OA's prefixes and store results as PreAnnotations."""
    with app.app_context():
        from s3_service import list_s3_images, get_object_bytes
        class_names = app.config["CLASS_NAMES"]

        job = _pre_annotate_jobs[oa_id]
        tag = f"[pre_annotate][OA {oa_id}][{model}]"
        log_every = max(1, _PRE_ANNOTATE_LOG_EVERY)
        try:
            print(f"{tag} starting — listing S3 images from {len(prefixes)} prefix(es)…", flush=True)
            # Collect all S3 keys from all prefixes
            all_keys = []
            for prefix in prefixes:
                for key in list_s3_images(bucket, prefix):
                    all_keys.append(key)

            # Filter out already pre-annotated keys (in batches to avoid huge IN queries)
            already_done = set()
            batch_size = 500
            for i in range(0, len(all_keys), batch_size):
                batch_keys = all_keys[i:i + batch_size]
                already_done.update(
                    r[0] for r in db.session.query(db.distinct(PreAnnotation.s3_key))
                    .filter(PreAnnotation.s3_key.in_(batch_keys)).all()
                )

            keys_to_process = [k for k in all_keys if k not in already_done]
            job["total"] = len(keys_to_process)
            job["already_done"] = len(already_done)
            job["grand_total"] = len(all_keys)
            print(f"{tag} {len(all_keys)} image(s) found, {len(already_done)} already done, "
                  f"{len(keys_to_process)} to process", flush=True)

            if not keys_to_process:
                job["status"] = "done"
                print(f"{tag} nothing to process — done.", flush=True)
                return

            total = len(keys_to_process)
            start_ts = time.time()
            for idx, s3_key in enumerate(keys_to_process, 1):
                # Check for stop signal
                if job.get("stop"):
                    job["status"] = "stopped"
                    print(f"{tag} STOPPED by user at {idx - 1}/{total} "
                          f"(ok={job['success']} failed={job['failed']} boxes={job['total_boxes']})",
                          flush=True)
                    return

                try:
                    img_bytes = get_object_bytes(bucket, s3_key)
                    filename = s3_key.split("/")[-1]
                    responded, converted = _detect_pre_annotation_boxes(
                        model, img_bytes, filename, class_names
                    )

                    if not responded:
                        job["failed"] += 1
                        job["processed"] += 1
                        print(f"{tag} {idx}/{total} FAILED (no model response): {s3_key}", flush=True)
                        continue

                    if converted:
                        for ann in converted:
                            db.session.add(PreAnnotation(
                                s3_key=s3_key,
                                class_id=ann["class_id"],
                                x_center=ann["x_center"],
                                y_center=ann["y_center"],
                                width=ann["width"],
                                height=ann["height"],
                                confidence=ann["confidence"],
                            ))
                            job["total_boxes"] += 1
                    else:
                        # Placeholder so it's marked as processed
                        db.session.add(PreAnnotation(
                            s3_key=s3_key, class_id=-1,
                            x_center=0, y_center=0, width=0, height=0, confidence=0,
                        ))

                    db.session.commit()
                    job["success"] += 1

                except Exception as e:
                    print(f"{tag} {idx}/{total} ERROR for {s3_key}: {e}", flush=True)
                    db.session.rollback()
                    job["failed"] += 1

                job["processed"] += 1

                # Periodic progress heartbeat (and always on the last item).
                if idx % log_every == 0 or idx == total:
                    elapsed = time.time() - start_ts
                    rate = idx / elapsed if elapsed > 0 else 0
                    eta = (total - idx) / rate if rate > 0 else 0
                    pct = idx * 100 // total
                    print(f"{tag} {idx}/{total} ({pct}%) | ok={job['success']} "
                          f"failed={job['failed']} boxes={job['total_boxes']} | "
                          f"{rate:.2f} img/s, ETA {eta/60:.1f} min", flush=True)

            job["status"] = "done"
            total_time = time.time() - start_ts
            print(f"{tag} COMPLETE — {job['success']} ok, {job['failed']} failed, "
                  f"{job['total_boxes']} boxes in {total_time/60:.1f} min", flush=True)
        except Exception as e:
            print(f"{tag} FATAL: {e}", flush=True)
            job["status"] = "error"
            job["error"] = str(e)
        finally:
            db.session.remove()


@admin_bp.route("/pre_annotate/<int:oa_id>", methods=["POST"])
@role_required("admin")
def pre_annotate(oa_id):
    """Start background pre-annotation (VGT, Gemini, or both) for all images
    in the OA's S3 prefixes."""
    oa = User.query.get_or_404(oa_id)
    if oa.role != "junior_oa":
        return jsonify({"error": "Can only pre-annotate for Junior OAs."}), 400

    model = (request.form.get("model") or "vgt").strip().lower()
    if model not in ("vgt", "gemini"):
        return jsonify({"error": "Invalid model. Choose vgt or gemini."}), 400

    if model == "gemini":
        from gemini_service import _get_api_key
        if not _get_api_key():
            return jsonify({"error": "Gemini not configured (set OPENROUTER_API_KEY)."}), 400

    existing_job = _pre_annotate_jobs.get(oa_id)
    if existing_job and existing_job["status"] == "running":
        return jsonify({"error": "Already running", "job": existing_job}), 409

    bucket = os.environ.get("AWS_S3_BUCKET", "").strip() or None
    if not bucket:
        return jsonify({"error": "S3 bucket not configured."}), 400

    cursors = OaCursor.query.filter_by(oa_id=oa_id).all()
    if not cursors:
        return jsonify({"error": "No S3 prefixes assigned."}), 400

    prefixes = [c.prefix for c in cursors]

    _pre_annotate_jobs[oa_id] = {
        "status": "running",
        "model": model,
        "processed": 0,
        "total": 0,
        "already_done": 0,
        "grand_total": 0,
        "success": 0,
        "failed": 0,
        "total_boxes": 0,
    }

    app = current_app._get_current_object()
    t = threading.Thread(target=_run_pre_annotate, args=(app, oa_id, bucket, prefixes, model), daemon=True)
    t.start()

    return jsonify({"status": "started", "total": 0})


@admin_bp.route("/pre_annotate_status/<int:oa_id>")
@role_required("admin")
def pre_annotate_status(oa_id):
    """Poll endpoint for pre-annotation progress."""
    job = _pre_annotate_jobs.get(oa_id)
    if not job:
        return jsonify({"status": "idle"})
    return jsonify(job)


@admin_bp.route("/pre_annotate_stop/<int:oa_id>", methods=["POST"])
@role_required("admin")
def pre_annotate_stop(oa_id):
    """Signal the background pre-annotation job to stop."""
    job = _pre_annotate_jobs.get(oa_id)
    if not job or job["status"] != "running":
        return jsonify({"error": "No running job to stop."}), 400
    job["stop"] = True
    return jsonify({"status": "stopping"})
