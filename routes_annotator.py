from flask import Blueprint, render_template, jsonify, request, current_app
from flask_login import current_user
from models import db, WorkItem
from auth import role_required

annotator_bp = Blueprint("annotator", __name__, url_prefix="/annotator")

PAGE_SIZE = 100


@annotator_bp.route("/grid")
@role_required("annotator")
def grid():
    return render_template("annotator/grid.html")


@annotator_bp.route("/grid_data")
@role_required("annotator")
def grid_data():
    page = request.args.get("page", 1, type=int)
    status_filter = request.args.get("status", "")

    query = WorkItem.query.filter_by(annotator_id=current_user.id)
    if status_filter:
        query = query.filter_by(status=status_filter)

    base = WorkItem.query.filter_by(annotator_id=current_user.id)
    total = base.count()
    pending = base.filter(WorkItem.status.in_(["pending", "rejected"])).count()
    done = base.filter(WorkItem.status.in_(["annotated", "approved"])).count()

    paginated = query.order_by(WorkItem.status.desc(), WorkItem.id)\
        .paginate(page=page, per_page=PAGE_SIZE, error_out=False)

    images = [
        {"id": w.id, "filename": w.filename, "status": w.status}
        for w in paginated.items
    ]

    return jsonify({
        "images": images,
        "total": total,
        "pending": pending,
        "done": done,
        "page": page,
        "pages": paginated.pages,
        "has_next": paginated.has_next,
    })


@annotator_bp.route("/annotate")
@role_required("annotator")
def annotate():
    item = WorkItem.query.filter_by(annotator_id=current_user.id)\
        .filter(WorkItem.status.in_(["pending", "rejected"])).first()
    image_id = item.id if item else None
    return render_template("annotator/annotate.html",
        image_id=image_id,
        class_names=current_app.config["CLASS_NAMES"],
    )


@annotator_bp.route("/annotate/<int:image_id>")
@role_required("annotator")
def annotate_image(image_id):
    item = WorkItem.query.filter_by(id=image_id, annotator_id=current_user.id).first()
    if not item:
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
    total = WorkItem.query.filter_by(annotator_id=current_user.id).count()
    done = WorkItem.query.filter_by(annotator_id=current_user.id)\
        .filter(WorkItem.status.in_(["annotated", "approved"])).count()
    return jsonify({"total": total, "done": done})
