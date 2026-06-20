import io
import os
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, redirect, Response
from flask_login import login_required, current_user
from PIL import Image
from models import db, WorkItem, Annotation, PreAnnotation
from auth import role_required
from s3_service import generate_presigned_url, get_object_bytes, put_object, object_exists, list_s3_folders

api_bp = Blueprint("api", __name__, url_prefix="/api")

THUMB_SIZE = 200
THUMB_PREFIX = "thumbnails/"


def _get_bucket():
    return os.environ.get("AWS_S3_BUCKET", "").strip() or None


def _image_key_to_label_key(s3_key):
    """Save label into an 'annotated' subfolder next to the image.

    e.g. path/to/image.jpg  ->  path/to/annotated/image.txt
    """
    folder = os.path.dirname(s3_key)
    stem = os.path.splitext(os.path.basename(s3_key))[0]
    return f"{folder}/annotated/{stem}.txt" if folder else f"annotated/{stem}.txt"


_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
}


def _image_response(raw_bytes, s3_key, max_age=3600):
    ext = os.path.splitext(s3_key)[1].lstrip(".").lower()
    mime = _MIME.get(ext, "image/jpeg")
    resp = Response(raw_bytes, mimetype=mime)
    resp.headers["Cache-Control"] = f"public, max-age={max_age}"
    return resp


@api_bp.route("/image/<int:image_id>")
@login_required
def serve_image(image_id):
    item = WorkItem.query.get_or_404(image_id)
    bucket = _get_bucket()
    if not bucket:
        return "S3 not configured", 503
    try:
        raw = get_object_bytes(bucket, item.s3_key)
    except Exception:
        return "Image not found", 404
    return _image_response(raw, item.s3_key, max_age=3600)


@api_bp.route("/thumbnail/<int:image_id>")
@login_required
def serve_thumbnail(image_id):
    item = WorkItem.query.get_or_404(image_id)
    bucket = _get_bucket()
    if not bucket:
        return "S3 not configured", 503

    thumb_key = THUMB_PREFIX + hashlib.md5(item.s3_key.encode()).hexdigest() + ".jpg"

    if not object_exists(bucket, thumb_key):
        try:
            raw = get_object_bytes(bucket, item.s3_key)
            pil_img = Image.open(io.BytesIO(raw))
            pil_img.thumbnail((THUMB_SIZE, THUMB_SIZE))
            buf = io.BytesIO()
            pil_img.save(buf, "JPEG", quality=75)
            buf.seek(0)
            put_object(bucket, thumb_key, buf.read(), content_type="image/jpeg")
        except Exception:
            return "Failed to create thumbnail", 500

    try:
        thumb_raw = get_object_bytes(bucket, thumb_key)
    except Exception:
        return "Thumbnail not found", 404
    return _image_response(thumb_raw, thumb_key, max_age=86400)


@api_bp.route("/image_meta/<int:image_id>")
@login_required
def image_meta(image_id):
    item = WorkItem.query.get_or_404(image_id)
    ann_name = item.annotator.username if item.annotator else None
    oa_name = item.oa.username if item.oa else None
    return jsonify({
        "id": item.id,
        "filename": item.filename,
        "status": item.status,
        "annotator": ann_name,
        "oa": oa_name,
        "annotated_at": item.annotated_at.isoformat() if item.annotated_at else None,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "reject_reason": item.reject_reason,
    })


@api_bp.route("/annotations/<int:image_id>")
@login_required
def get_annotations(image_id):
    item = WorkItem.query.get_or_404(image_id)
    anns = Annotation.query.filter_by(s3_key=item.s3_key).all()

    # If no manual annotations exist and item is pending, fall back to pre-annotations
    if not anns and item.status in ("pending", "rejected"):
        pre_anns = PreAnnotation.query.filter(
            PreAnnotation.s3_key == item.s3_key,
            PreAnnotation.class_id >= 0,
        ).all()
        if pre_anns:
            return jsonify({
                "annotations": [{
                    "class_id": a.class_id,
                    "x_center": a.x_center,
                    "y_center": a.y_center,
                    "width": a.width,
                    "height": a.height,
                } for a in pre_anns],
                "source": "pre_annotation",
            })

    return jsonify({
        "annotations": [{
            "class_id": a.class_id,
            "x_center": a.x_center,
            "y_center": a.y_center,
            "width": a.width,
            "height": a.height,
        } for a in anns],
        "source": "annotation",
    })


@api_bp.route("/annotations/<int:image_id>", methods=["POST"])
@login_required
def save_annotations(image_id):
    item = WorkItem.query.get_or_404(image_id)
    data = request.get_json() or {}
    boxes = data.get("annotations", [])

    Annotation.query.filter_by(s3_key=item.s3_key).delete()
    for box in boxes:
        db.session.add(Annotation(
            s3_key=item.s3_key,
            class_id=int(box["class_id"]),
            x_center=float(box["x_center"]),
            y_center=float(box["y_center"]),
            width=float(box["width"]),
            height=float(box["height"]),
        ))
    db.session.commit()
    return jsonify({"status": "success"})


@api_bp.route("/save_and_next", methods=["POST"])
@login_required
def save_and_next():
    data = request.get_json() or {}
    image_id = data.get("image_id")
    boxes = data.get("annotations", [])

    if not image_id:
        return jsonify({"status": "error", "message": "No image_id"}), 400

    item = WorkItem.query.get_or_404(image_id)

    Annotation.query.filter_by(s3_key=item.s3_key).delete()
    for box in boxes:
        db.session.add(Annotation(
            s3_key=item.s3_key,
            class_id=int(box["class_id"]),
            x_center=float(box["x_center"]),
            y_center=float(box["y_center"]),
            width=float(box["width"]),
            height=float(box["height"]),
        ))

    if item.status in ("pending", "rejected"):
        item.status = "annotated"
        item.annotated_at = datetime.now(timezone.utc)

    db.session.commit()

    next_item = WorkItem.query.filter_by(
        annotator_id=current_user.id, status="pending"
    ).first() or WorkItem.query.filter_by(
        annotator_id=current_user.id, status="rejected"
    ).first()

    if next_item:
        return jsonify({"status": "ok", "next_image_id": next_item.id})
    return jsonify({"status": "done", "message": "No more images to annotate"})


@api_bp.route("/s3/browse")
@login_required
def s3_browse():
    """Return immediate sub-folders under a given S3 prefix for the browser UI."""
    prefix = request.args.get("prefix", "")
    bucket = _get_bucket()
    if not bucket:
        return jsonify({"error": "S3 bucket not configured"}), 503
    try:
        folders, has_images = list_s3_folders(bucket, prefix)
        # Strip trailing slash and get just the folder name for display
        display = [{"prefix": f, "name": f.rstrip("/").split("/")[-1]} for f in folders]
        parent = "/".join(prefix.rstrip("/").split("/")[:-1]) + "/" if "/" in prefix.rstrip("/") else ""
        return jsonify({
            "bucket": bucket,
            "current": prefix,
            "parent": parent if prefix else None,
            "folders": display,
            "has_images": has_images,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _save_annotations_for_key(s3_key, boxes):
    """Replace all annotations for s3_key with the provided boxes."""
    Annotation.query.filter_by(s3_key=s3_key).delete()
    for box in boxes:
        db.session.add(Annotation(
            s3_key=s3_key,
            class_id=int(box["class_id"]),
            x_center=float(box["x_center"]),
            y_center=float(box["y_center"]),
            width=float(box["width"]),
            height=float(box["height"]),
        ))


def _write_label_to_s3(item):
    """Write YOLO label file to S3 at path/annotated/stem.txt."""
    bucket = _get_bucket()
    if not bucket:
        return
    anns = Annotation.query.filter_by(s3_key=item.s3_key).all()
    lines = [
        f"{a.class_id} {a.x_center:.6f} {a.y_center:.6f} {a.width:.6f} {a.height:.6f}"
        for a in anns
    ]
    label_key = _image_key_to_label_key(item.s3_key)
    try:
        put_object(bucket, label_key,
                   ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                   content_type="text/plain")
    except Exception:
        import traceback; traceback.print_exc()


@api_bp.route("/review/<int:image_id>", methods=["POST"])
@login_required
def review_image(image_id):
    """
    Two-level OA review — both levels can edit annotations before approving.
    Junior OA  (role=junior_oa) : annotated / rejected_by_senior → junior_approved
    Senior OA  (role=senior_oa) : junior_approved → approved  (+ S3 label write)
                                  junior_approved → rejected_by_senior (send back)
    """
    if current_user.role not in ("junior_oa", "senior_oa"):
        return jsonify({"status": "error", "message": "Forbidden"}), 403

    data = request.get_json() or {}
    action = data.get("action")
    if action not in ("approve", "reject"):
        return jsonify({"status": "error", "message": "Invalid action"}), 400

    annotations = data.get("annotations", [])

    item = WorkItem.query.filter_by(id=image_id).first()
    if not item:
        return jsonify({"status": "error", "message": "Not found"}), 404

    if current_user.role == "junior_oa":
        if item.oa_id != current_user.id:
            return jsonify({"status": "error", "message": "Not your assignment"}), 403
        if item.status not in ("annotated", "rejected_by_senior"):
            return jsonify({"status": "error", "message": "Item is not reviewable"}), 400

        _save_annotations_for_key(item.s3_key, annotations)
        item.status = "junior_approved"
        item.reject_reason = None
        item.reviewed_at = datetime.now(timezone.utc)
        db.session.commit()

        # Determine which queue to pull next from based on current review mode
        next_status = "rejected_by_senior" if data.get("review_mode") == "rejected_by_senior" else "annotated"
        next_query = WorkItem.query.filter_by(oa_id=current_user.id, status=next_status)
        annotator_filter = data.get("annotator_id")
        if annotator_filter:
            next_query = next_query.filter_by(annotator_id=int(annotator_filter))
        next_item = next_query.first()

    else:  # senior_oa
        from models import SeniorJuniorOa
        if item.oa_id:
            link = SeniorJuniorOa.query.filter_by(
                senior_oa_id=current_user.id, junior_oa_id=item.oa_id
            ).first()
            if not link:
                return jsonify({"status": "error", "message": "Not your assignment"}), 403
        if item.status != "junior_approved":
            return jsonify({"status": "error", "message": "Item is not junior_approved"}), 400

        if action == "reject":
            # Send back to junior QA for correction
            item.status = "rejected_by_senior"
            item.reject_reason = data.get("reject_reason", "")
            item.reviewed_at = datetime.now(timezone.utc)
            db.session.commit()
        else:
            _save_annotations_for_key(item.s3_key, annotations)
            item.status = "approved"
            item.reviewed_at = datetime.now(timezone.utc)
            db.session.commit()
            _write_label_to_s3(item)

        links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
        junior_ids = [l.junior_oa_id for l in links]
        next_item = (
            WorkItem.query.filter(
                WorkItem.oa_id.in_(junior_ids),
                WorkItem.status == "junior_approved"
            ).first()
            if junior_ids else None
        )

    if next_item:
        return jsonify({"status": "ok", "next_image_id": next_item.id})
    return jsonify({"status": "done", "message": "No more images to review"})


@api_bp.route("/navigate_annotator")
@login_required
def navigate_annotator():
    """Return prev/next image IDs for the current annotator (no save)."""
    current_id = request.args.get("current_id", type=int)
    direction = request.args.get("direction", "next")  # "next" or "prev"

    my_items = WorkItem.query.filter(
        WorkItem.annotator_id == current_user.id,
    ).order_by(WorkItem.id)

    if direction == "next":
        item = my_items.filter(WorkItem.id > current_id).first()
    else:
        item = my_items.filter(WorkItem.id < current_id).order_by(WorkItem.id.desc()).first()

    return jsonify({"image_id": item.id if item else None})


@api_bp.route("/peek_next")
@login_required
def peek_next():
    """Return the next pending/rejected image ID for the current annotator.
    Used for prefetching — no side effects, no state change.
    """
    current_id = request.args.get("current_id", type=int)
    item = WorkItem.query.filter(
        WorkItem.annotator_id == current_user.id,
        WorkItem.status == "pending",
        WorkItem.id != current_id,
    ).first()
    if not item:
        item = WorkItem.query.filter(
            WorkItem.annotator_id == current_user.id,
            WorkItem.status == "rejected",
            WorkItem.id != current_id,
        ).first()
    return jsonify({"next_image_id": item.id if item else None})


@api_bp.route("/peek_next_review")
@login_required
def peek_next_review():
    """Return the next reviewable image ID for Junior OA or Senior OA.
    No side effects — used only for prefetching.
    """
    current_id = request.args.get("current_id", type=int)
    item = None

    if current_user.role == "junior_oa":
        review_mode = request.args.get("review_mode", "annotated")
        target_status = "rejected_by_senior" if review_mode == "rejected_by_senior" else "annotated"
        q = WorkItem.query.filter(
            WorkItem.oa_id == current_user.id,
            WorkItem.status == target_status,
            WorkItem.id != current_id,
        )
        annotator_filter = request.args.get("annotator_id", type=int)
        if annotator_filter:
            q = q.filter_by(annotator_id=annotator_filter)
        item = q.first()

    elif current_user.role == "senior_oa":
        from models import SeniorJuniorOa
        links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
        junior_ids = [l.junior_oa_id for l in links]
        if junior_ids:
            item = WorkItem.query.filter(
                WorkItem.oa_id.in_(junior_ids),
                WorkItem.status == "junior_approved",
                WorkItem.id != current_id,
            ).first()

    return jsonify({"next_image_id": item.id if item else None})


@api_bp.route("/navigate_review")
@login_required
def navigate_review():
    """Return prev/next image ID for OA / Senior OA review pages (no save)."""
    current_id = request.args.get("current_id", type=int)
    direction = request.args.get("direction", "next")  # "next" or "prev"
    annotator_filter = request.args.get("annotator_id", type=int)

    q = None
    if current_user.role == "junior_oa":
        q = WorkItem.query.filter(WorkItem.oa_id == current_user.id)
        if annotator_filter:
            q = q.filter_by(annotator_id=annotator_filter)

    elif current_user.role == "senior_oa":
        from models import SeniorJuniorOa
        links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
        junior_ids = [l.junior_oa_id for l in links]
        if junior_ids:
            q = WorkItem.query.filter(WorkItem.oa_id.in_(junior_ids))
        else:
            return jsonify({"image_id": None})

    if q is None:
        return jsonify({"image_id": None})

    if direction == "next":
        item = q.filter(WorkItem.id > current_id).order_by(WorkItem.id).first()
    else:
        item = q.filter(WorkItem.id < current_id).order_by(WorkItem.id.desc()).first()

    return jsonify({"image_id": item.id if item else None})


@api_bp.route("/cursor_count/<int:cursor_id>")
@login_required
def cursor_count(cursor_id):
    """Poll endpoint — returns total_images for a cursor (null if still counting).
    Accessible by admin (any cursor) or OA (own cursors only).
    """
    from models import OaCursor
    cursor = OaCursor.query.get_or_404(cursor_id)
    if current_user.role not in ("admin",) and cursor.oa_id != current_user.id:
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"cursor_id": cursor_id, "total_images": cursor.total_images})


