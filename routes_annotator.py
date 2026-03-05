from flask import Blueprint, render_template, jsonify, current_app
from flask_login import current_user
from models import db, Assignment, ImageRecord
from auth import role_required

annotator_bp = Blueprint("annotator", __name__, url_prefix="/annotator")


@annotator_bp.route("/grid")
@role_required("annotator")
def grid():
    return render_template("annotator/grid.html")


@annotator_bp.route("/grid_data")
@role_required("annotator")
def grid_data():
    assignments = Assignment.query.filter_by(annotator_id=current_user.id)\
        .order_by(Assignment.status.desc(), Assignment.id).all()
    images = []
    for a in assignments:
        img = ImageRecord.query.get(a.image_id)
        if img:
            images.append({
                "id": img.id,
                "filename": img.filename,
                "status": a.status,
            })
    # Counts
    total = len(images)
    pending = sum(1 for i in images if i["status"] in ("pending", "rejected"))
    done = sum(1 for i in images if i["status"] in ("annotated", "approved"))
    return jsonify({"images": images, "total": total, "pending": pending, "done": done})


@annotator_bp.route("/annotate")
@role_required("annotator")
def annotate():
    # Find next pending/rejected image
    assignment = Assignment.query.filter_by(
        annotator_id=current_user.id
    ).filter(Assignment.status.in_(["pending", "rejected"])).first()

    image_id = assignment.image_id if assignment else None
    return render_template("annotator/annotate.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@annotator_bp.route("/annotate/<int:image_id>")
@role_required("annotator")
def annotate_image(image_id):
    # Verify this image is assigned to the annotator
    assignment = Assignment.query.filter_by(
        image_id=image_id,
        annotator_id=current_user.id,
    ).first()
    if not assignment:
        from flask import flash, redirect, url_for
        flash("Image not assigned to you.", "danger")
        return redirect(url_for("annotator.grid"))

    return render_template("annotator/annotate.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@annotator_bp.route("/progress")
@role_required("annotator")
def progress():
    total = Assignment.query.filter_by(annotator_id=current_user.id).count()
    done = Assignment.query.filter_by(annotator_id=current_user.id).filter(
        Assignment.status.in_(["annotated", "approved"])
    ).count()
    return jsonify({"total": total, "done": done})
