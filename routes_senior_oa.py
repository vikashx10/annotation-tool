from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import current_user
from models import db, User, SeniorJuniorOa, WorkItem
from auth import role_required

senior_oa_bp = Blueprint("senior_oa", __name__, url_prefix="/senior_oa")


@senior_oa_bp.route("/")
@role_required("senior_oa")
def dashboard():
    links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
    junior_ids = [l.junior_oa_id for l in links]
    managed_juniors = User.query.filter(User.id.in_(junior_ids)).all() if junior_ids else []

    all_juniors = User.query.filter_by(role="junior_oa").all()
    available_juniors = [j for j in all_juniors if j.id not in junior_ids]

    # Queue: junior_approved items from managed junior OAs
    to_review = (
        WorkItem.query.filter(
            WorkItem.oa_id.in_(junior_ids),
            WorkItem.status == "junior_approved"
        ).count()
        if junior_ids else 0
    )
    total_approved = (
        WorkItem.query.filter(
            WorkItem.oa_id.in_(junior_ids),
            WorkItem.status == "approved"
        ).count()
        if junior_ids else 0
    )

    junior_stats = []
    for jr in managed_juniors:
        junior_approved = WorkItem.query.filter_by(oa_id=jr.id, status="junior_approved").count()
        approved = WorkItem.query.filter_by(oa_id=jr.id, status="approved").count()
        total = WorkItem.query.filter_by(oa_id=jr.id).count()
        junior_stats.append({
            "user": jr,
            "junior_approved": junior_approved,
            "approved": approved,
            "total": total,
        })

    return render_template("senior_oa/dashboard.html",
        managed_juniors=managed_juniors,
        available_juniors=available_juniors,
        junior_stats=junior_stats,
        to_review=to_review,
        total_approved=total_approved,
    )


@senior_oa_bp.route("/add_junior", methods=["POST"])
@role_required("senior_oa")
def add_junior():
    junior_id = request.form.get("junior_id", type=int)
    if not junior_id:
        flash("Select a Junior OA.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    user = User.query.get(junior_id)
    if not user or user.role != "junior_oa":
        flash("Invalid Junior OA.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    if SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id, junior_oa_id=junior_id).first():
        flash("Already managing this Junior OA.", "warning")
        return redirect(url_for("senior_oa.dashboard"))

    db.session.add(SeniorJuniorOa(senior_oa_id=current_user.id, junior_oa_id=junior_id))
    db.session.commit()
    flash(f"Added Junior OA '{user.username}'.", "success")
    return redirect(url_for("senior_oa.dashboard"))


@senior_oa_bp.route("/remove_junior/<int:junior_id>", methods=["POST"])
@role_required("senior_oa")
def remove_junior(junior_id):
    link = SeniorJuniorOa.query.filter_by(
        senior_oa_id=current_user.id, junior_oa_id=junior_id
    ).first()
    if link:
        db.session.delete(link)
        db.session.commit()
        flash("Junior OA removed.", "success")
    return redirect(url_for("senior_oa.dashboard"))


@senior_oa_bp.route("/review")
@role_required("senior_oa")
def review():
    links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
    junior_ids = [l.junior_oa_id for l in links]
    item = (
        WorkItem.query.filter(
            WorkItem.oa_id.in_(junior_ids),
            WorkItem.status == "junior_approved"
        ).first()
        if junior_ids else None
    )
    image_id = item.id if item else None
    return render_template("senior_oa/review.html",
        image_id=image_id,
        class_names=__import__('flask').current_app.config["CLASS_NAMES"],
    )


@senior_oa_bp.route("/review/<int:image_id>")
@role_required("senior_oa")
def review_image(image_id):
    links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
    junior_ids = [l.junior_oa_id for l in links]
    item = WorkItem.query.filter(
        WorkItem.id == image_id,
        WorkItem.oa_id.in_(junior_ids)
    ).first()
    if not item:
        flash("Image not accessible.", "danger")
        return redirect(url_for("senior_oa.dashboard"))
    return render_template("senior_oa/review.html",
        image_id=image_id,
        class_names=__import__('flask').current_app.config["CLASS_NAMES"],
    )
