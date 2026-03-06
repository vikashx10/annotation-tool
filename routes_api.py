import io
import os
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, redirect
from flask_login import login_required, current_user
from PIL import Image
from models import db, WorkItem, Annotation
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


@api_bp.route("/image/<int:image_id>")
@login_required
def serve_image(image_id):
    item = WorkItem.query.get_or_404(image_id)
    bucket = _get_bucket()
    if not bucket:
        return "S3 not configured", 503
    url = generate_presigned_url(bucket, item.s3_key)
    return redirect(url)


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

    url = generate_presigned_url(bucket, thumb_key)
    return redirect(url)


@api_bp.route("/annotations/<int:image_id>")
@login_required
def get_annotations(image_id):
    item = WorkItem.query.get_or_404(image_id)
    anns = Annotation.query.filter_by(s3_key=item.s3_key).all()
    return jsonify([{
        "class_id": a.class_id,
        "x_center": a.x_center,
        "y_center": a.y_center,
        "width": a.width,
        "height": a.height,
    } for a in anns])


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


@api_bp.route("/review/<int:image_id>", methods=["POST"])
@role_required("oa")
def review_image(image_id):
    data = request.get_json() or {}
    action = data.get("action")

    item = WorkItem.query.filter_by(id=image_id).first()
    if not item:
        return jsonify({"status": "error", "message": "Not found"}), 404

    if item.oa_id != current_user.id:
        return jsonify({"status": "error", "message": "Not your assignment"}), 403

    if action == "approve":
        item.status = "approved"
        item.reviewed_at = datetime.now(timezone.utc)

        # Write YOLO label file to S3 next to the image
        bucket = _get_bucket()
        if bucket:
            anns = Annotation.query.filter_by(s3_key=item.s3_key).all()
            lines = [
                f"{a.class_id} {a.x_center:.6f} {a.y_center:.6f} {a.width:.6f} {a.height:.6f}"
                for a in anns
            ]
            label_content = "\n".join(lines) + ("\n" if lines else "")
            label_key = _image_key_to_label_key(item.s3_key)
            try:
                put_object(bucket, label_key, label_content.encode("utf-8"), content_type="text/plain")
            except Exception as e:
                # Don't block approval if S3 write fails — log and continue
                import traceback; traceback.print_exc()

    elif action == "reject":
        item.status = "rejected"
        item.reviewed_at = datetime.now(timezone.utc)
    else:
        return jsonify({"status": "error", "message": "Invalid action"}), 400

    db.session.commit()

    next_item = WorkItem.query.filter_by(oa_id=current_user.id, status="annotated").first()
    if next_item:
        return jsonify({"status": "ok", "next_image_id": next_item.id})
    return jsonify({"status": "done", "message": "No more images to review"})


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


