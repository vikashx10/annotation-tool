from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import current_user
from models import db, User, OaAnnotator, Assignment, ImageRecord
from auth import role_required

oa_bp = Blueprint("oa", __name__, url_prefix="/oa")


@oa_bp.route("/")
@role_required("oa")
def dashboard():
    # Get annotators managed by this OA
    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    annotator_ids = [l.annotator_id for l in links]
    managed_annotators = User.query.filter(User.id.in_(annotator_ids)).all() if annotator_ids else []

    # All annotators (for adding)
    all_annotators = User.query.filter_by(role="annotator").all()
    available_annotators = [a for a in all_annotators if a.id not in annotator_ids]

    # Stats
    total_assigned = Assignment.query.filter_by(oa_id=current_user.id).count()
    total_annotated = Assignment.query.filter_by(oa_id=current_user.id, status="annotated").count()
    total_approved = Assignment.query.filter_by(oa_id=current_user.id, status="approved").count()
    total_rejected = Assignment.query.filter_by(oa_id=current_user.id, status="rejected").count()
    total_pending = Assignment.query.filter_by(oa_id=current_user.id, status="pending").count()

    # Per-annotator stats
    annotator_stats = []
    for ann in managed_annotators:
        assigned = Assignment.query.filter_by(oa_id=current_user.id, annotator_id=ann.id).count()
        done = Assignment.query.filter_by(oa_id=current_user.id, annotator_id=ann.id)\
            .filter(Assignment.status.in_(["annotated", "approved"])).count()
        annotator_stats.append({"user": ann, "assigned": assigned, "done": done})

    # Unassigned to annotator count
    unassigned_to_annotator = Assignment.query.filter_by(
        oa_id=current_user.id, annotator_id=None
    ).count()

    return render_template("oa/dashboard.html",
        managed_annotators=managed_annotators,
        available_annotators=available_annotators,
        annotator_stats=annotator_stats,
        total_assigned=total_assigned,
        total_annotated=total_annotated,
        total_approved=total_approved,
        total_rejected=total_rejected,
        total_pending=total_pending,
        unassigned_to_annotator=unassigned_to_annotator,
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
        # Unassign their pending images back to pool
        Assignment.query.filter_by(
            oa_id=current_user.id, annotator_id=annotator_id, status="pending"
        ).update({"annotator_id": None})
        db.session.delete(link)
        db.session.commit()
        flash("Annotator removed.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/distribute", methods=["POST"])
@role_required("oa")
def distribute():
    """Distribute specific number of images to each annotator (randomly)."""
    import random

    links = OaAnnotator.query.filter_by(oa_id=current_user.id).all()
    if not links:
        flash("Add annotators first.", "danger")
        return redirect(url_for("oa.dashboard"))

    # Find images assigned to this OA but not to any annotator
    unassigned = Assignment.query.filter_by(
        oa_id=current_user.id, annotator_id=None
    ).order_by(Assignment.id).all()

    if not unassigned:
        flash("No unassigned images to distribute.", "danger")
        return redirect(url_for("oa.dashboard"))

    random.shuffle(unassigned)

    total_distributed = 0
    remaining = list(unassigned)

    for link in links:
        count = request.form.get(f"count_{link.annotator_id}", 0, type=int)
        if count <= 0:
            continue
        to_assign = remaining[:count]
        remaining = remaining[count:]
        for assignment in to_assign:
            assignment.annotator_id = link.annotator_id
        total_distributed += len(to_assign)

    db.session.commit()
    flash(f"Randomly distributed {total_distributed} images. {len(remaining)} still unassigned.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/deselect/<int:annotator_id>", methods=["POST"])
@role_required("oa")
def deselect_annotator(annotator_id):
    """Remove un-annotated images from an annotator (only pending ones)."""
    count = Assignment.query.filter_by(
        oa_id=current_user.id, annotator_id=annotator_id, status="pending"
    ).update({"annotator_id": None})
    db.session.commit()
    flash(f"Deselected {count} pending images from annotator.", "success")
    return redirect(url_for("oa.dashboard"))


@oa_bp.route("/review")
@role_required("oa")
def review():
    # Find next annotated image to review
    assignment = Assignment.query.filter_by(
        oa_id=current_user.id, status="annotated"
    ).first()
    image_id = assignment.image_id if assignment else None
    return render_template("oa/review.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@oa_bp.route("/review/<int:image_id>")
@role_required("oa")
def review_image(image_id):
    assignment = Assignment.query.filter_by(
        image_id=image_id, oa_id=current_user.id
    ).first()
    if not assignment:
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
    assignments = Assignment.query.filter_by(oa_id=current_user.id)\
        .order_by(Assignment.status.desc(), Assignment.id).all()
    images = []
    for a in assignments:
        img = ImageRecord.query.get(a.image_id)
        if img:
            images.append({
                "id": img.id,
                "filename": img.filename,
                "status": a.status,
                "annotator_id": a.annotator_id,
            })
    total = len(images)
    to_review = sum(1 for i in images if i["status"] == "annotated")
    approved = sum(1 for i in images if i["status"] == "approved")
    return jsonify({"images": images, "total": total, "to_review": to_review, "approved": approved})
