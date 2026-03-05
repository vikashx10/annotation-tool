import os
import hashlib
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, current_app, make_response
from flask_login import login_required, current_user
from PIL import Image
from models import db, ImageRecord, Assignment, Annotation, Config
from auth import role_required

api_bp = Blueprint("api", __name__, url_prefix="/api")

THUMB_SIZE = 200


def _get_image_root():
    cfg = Config.query.get("image_root")
    return cfg.value if cfg else None


def _image_full_path(relative_path):
    root = _get_image_root()
    if not root:
        return None
    return os.path.join(root, relative_path)


@api_bp.route("/image/<int:image_id>")
@login_required
def serve_image(image_id):
    img = ImageRecord.query.get_or_404(image_id)
    full_path = _image_full_path(img.relative_path)
    if not full_path or not os.path.exists(full_path):
        return "Image not found", 404
    response = make_response(send_file(full_path))
    response.headers["Cache-Control"] = "public, max-age=3600, immutable"
    return response


@api_bp.route("/thumbnail/<int:image_id>")
@login_required
def serve_thumbnail(image_id):
    img = ImageRecord.query.get_or_404(image_id)
    thumb_dir = os.path.join(current_app.root_path, "static", "thumbnails")
    os.makedirs(thumb_dir, exist_ok=True)

    # Use hash of relative path for cache key
    thumb_name = hashlib.md5(img.relative_path.encode()).hexdigest() + ".jpg"
    thumb_path = os.path.join(thumb_dir, thumb_name)

    if not os.path.exists(thumb_path):
        full_path = _image_full_path(img.relative_path)
        if not full_path or not os.path.exists(full_path):
            return "Image not found", 404
        try:
            pil_img = Image.open(full_path)
            pil_img.thumbnail((THUMB_SIZE, THUMB_SIZE))
            pil_img.save(thumb_path, "JPEG", quality=75)
        except Exception:
            return "Failed to create thumbnail", 500

    response = make_response(send_file(thumb_path, mimetype="image/jpeg"))
    response.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return response


@api_bp.route("/annotations/<int:image_id>")
@login_required
def get_annotations(image_id):
    anns = Annotation.query.filter_by(image_id=image_id).all()
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
    data = request.get_json() or {}
    boxes = data.get("annotations", [])

    # Delete existing annotations
    Annotation.query.filter_by(image_id=image_id).delete()

    # Insert new
    for box in boxes:
        db.session.add(Annotation(
            image_id=image_id,
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

    # Save annotations
    Annotation.query.filter_by(image_id=image_id).delete()
    for box in boxes:
        db.session.add(Annotation(
            image_id=image_id,
            class_id=int(box["class_id"]),
            x_center=float(box["x_center"]),
            y_center=float(box["y_center"]),
            width=float(box["width"]),
            height=float(box["height"]),
        ))

    # Mark as annotated
    from datetime import datetime, timezone
    assignment = Assignment.query.filter_by(image_id=image_id).first()
    if assignment and assignment.status in ("pending", "rejected"):
        assignment.status = "annotated"
        assignment.annotated_at = datetime.now(timezone.utc)

    db.session.commit()

    # Find next pending image for this annotator
    next_assignment = Assignment.query.filter_by(
        annotator_id=current_user.id,
        status="pending"
    ).first()

    # Also check rejected images
    if not next_assignment:
        next_assignment = Assignment.query.filter_by(
            annotator_id=current_user.id,
            status="rejected"
        ).first()

    if next_assignment:
        return jsonify({"status": "ok", "next_image_id": next_assignment.image_id})
    return jsonify({"status": "done", "message": "No more images to annotate"})


@api_bp.route("/review/<int:image_id>", methods=["POST"])
@role_required("oa")
def review_image(image_id):
    data = request.get_json() or {}
    action = data.get("action")  # "approve" or "reject"

    assignment = Assignment.query.filter_by(image_id=image_id).first()
    if not assignment:
        return jsonify({"status": "error", "message": "No assignment found"}), 404

    if assignment.oa_id != current_user.id:
        return jsonify({"status": "error", "message": "Not your assignment"}), 403

    from datetime import datetime, timezone
    if action == "approve":
        assignment.status = "approved"
        assignment.reviewed_at = datetime.now(timezone.utc)
    elif action == "reject":
        assignment.status = "rejected"
        assignment.reviewed_at = datetime.now(timezone.utc)
    else:
        return jsonify({"status": "error", "message": "Invalid action"}), 400

    db.session.commit()

    # Find next image to review
    next_assignment = Assignment.query.filter_by(
        oa_id=current_user.id,
        status="annotated"
    ).first()

    if next_assignment:
        return jsonify({"status": "ok", "next_image_id": next_assignment.image_id})
    return jsonify({"status": "done", "message": "No more images to review"})


@api_bp.route("/browse")
@login_required
def browse_dirs():
    raw = request.args.get("path", "").strip()
    if not raw:
        raw = str(Path.home())

    target = Path(raw).resolve()
    if not target.exists() or not target.is_dir():
        return jsonify({"status": "error", "message": f"Not a valid directory: {target}"}), 400

    subdirs = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                subdirs.append(entry.name)
    except PermissionError:
        return jsonify({"status": "error", "message": f"Permission denied: {target}"}), 403

    return jsonify({
        "current": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "dirs": subdirs,
    })
