import os
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import current_user
from models import db, User, OaAnnotator, OaCursor, SeniorJuniorOa, WorkItem, Annotation
from auth import role_required
from s3_service import put_object

senior_oa_bp = Blueprint("senior_oa", __name__, url_prefix="/senior_oa")

# Items awaiting senior review. Both the human-first path and the Gemini
# model-annotation path converge on 'junior_approved' once the junior signs off.
AWAITING_SENIOR = ("junior_approved",)


@senior_oa_bp.route("/")
@role_required("senior_oa")
def dashboard():
    links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
    junior_ids = [l.junior_oa_id for l in links]
    managed_juniors = User.query.filter(User.id.in_(junior_ids)).all() if junior_ids else []

    # Junior OAs this senior created. Creating one links it to the team, so this
    # is also the set they oversee.
    owned_juniors = User.query.filter_by(
        role="junior_oa", senior_oa_id=current_user.id
    ).order_by(User.username).all()

    # Queue: junior_approved items from managed junior OAs
    to_review = (
        WorkItem.query.filter(
            WorkItem.oa_id.in_(junior_ids),
            WorkItem.status.in_(AWAITING_SENIOR)
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
    total_sent_back = (
        WorkItem.query.filter(
            WorkItem.oa_id.in_(junior_ids),
            WorkItem.status == "rejected_by_senior"
        ).count()
        if junior_ids else 0
    )

    junior_stats = []
    for jr in managed_juniors:
        awaiting = WorkItem.query.filter(
            WorkItem.oa_id == jr.id, WorkItem.status.in_(AWAITING_SENIOR)
        ).count()
        approved = WorkItem.query.filter_by(oa_id=jr.id, status="approved").count()
        sent_back = WorkItem.query.filter_by(oa_id=jr.id, status="rejected_by_senior").count()
        total = WorkItem.query.filter_by(oa_id=jr.id).count()
        junior_stats.append({
            "user": jr,
            "awaiting": awaiting,
            "junior_approved": awaiting + approved,
            "approved": approved,
            "sent_back": sent_back,
            "total": total,
        })

    # Juniors this senior created who are not on their team yet. Creating one
    # links it immediately, so this is normally empty — it fills up when an
    # admin assigns ownership of an existing account.
    on_team = set(junior_ids)
    available_juniors = [j for j in owned_juniors if j.id not in on_team]

    # NOTE: no key here may be named `items`/`keys`/`values` — in Jinja a dict
    # attribute lookup finds the built-in method before the key, so `stat.items`
    # would render "<built-in method items of dict object ...>".
    owned_junior_stats = [
        {
            "user": j,
            "on_team": j.id in on_team,
            "annotators": OaAnnotator.query.filter_by(oa_id=j.id).count(),
            "work_items": WorkItem.query.filter_by(oa_id=j.id).count(),
        }
        for j in owned_juniors
    ]

    return render_template("senior_oa/dashboard.html",
        managed_juniors=managed_juniors,
        junior_stats=junior_stats,
        owned_junior_stats=owned_junior_stats,
        available_juniors=available_juniors,
        to_review=to_review,
        total_approved=total_approved,
        total_sent_back=total_sent_back,
    )


@senior_oa_bp.route("/add_junior", methods=["POST"])
@role_required("senior_oa")
def add_junior():
    """Add a Junior OA this senior created to their team."""
    junior_id = request.form.get("junior_id", type=int)
    if not junior_id:
        flash("Select a Junior OA.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    user = User.query.filter_by(
        id=junior_id, role="junior_oa", senior_oa_id=current_user.id
    ).first()
    if not user:
        flash("That Junior OA was not created by you.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    if SeniorJuniorOa.query.filter_by(
            senior_oa_id=current_user.id, junior_oa_id=junior_id).first():
        flash("Already on your team.", "warning")
        return redirect(url_for("senior_oa.dashboard"))

    db.session.add(SeniorJuniorOa(senior_oa_id=current_user.id, junior_oa_id=junior_id))
    db.session.commit()
    flash(f"Added Junior OA '{user.username}'.", "success")
    return redirect(url_for("senior_oa.dashboard"))


@senior_oa_bp.route("/create_junior", methods=["POST"])
@role_required("senior_oa")
def create_junior():
    """Create a Junior OA account owned by this Senior OA.

    Ownership is what scopes the account: only the senior who created a Junior
    OA can add them to their team, and the new account is added straight away
    so it is immediately usable.
    """
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        flash("Username and password are required.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    if len(password) < 4:
        flash("Password must be at least 4 characters.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    if User.query.filter_by(username=username).first():
        flash(f"Username '{username}' is already taken.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    user = User(username=username, role="junior_oa", senior_oa_id=current_user.id)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()          # need the id for the team link below
    db.session.add(SeniorJuniorOa(senior_oa_id=current_user.id, junior_oa_id=user.id))
    db.session.commit()
    flash(f"Junior OA '{username}' created and added to your team.", "success")
    return redirect(url_for("senior_oa.dashboard"))


@senior_oa_bp.route("/delete_junior/<int:junior_id>", methods=["POST"])
@role_required("senior_oa")
def delete_junior(junior_id):
    """Delete a Junior OA account this senior created.

    Mirrors admin.delete_user's junior_oa branch: finished work is preserved
    with its oa_id cleared so it still exports, un-annotated work is dropped,
    and the S3 prefixes are freed for reassignment.
    """
    user = User.query.filter_by(
        id=junior_id, role="junior_oa", senior_oa_id=current_user.id
    ).first()
    if not user:
        flash("That Junior OA was not created by you.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    username = user.username

    # Keep anything annotated or beyond — it still has to export.
    WorkItem.query.filter(
        WorkItem.oa_id == junior_id,
        WorkItem.status.in_(["annotated", "junior_approved", "approved"])
    ).update({"oa_id": None, "annotator_id": None}, synchronize_session=False)

    # Nothing of value in un-annotated work.
    WorkItem.query.filter(
        WorkItem.oa_id == junior_id,
        WorkItem.status.in_(["pending", "rejected", "rejected_by_senior"])
    ).delete(synchronize_session=False)

    OaCursor.query.filter_by(oa_id=junior_id).delete(synchronize_session=False)
    OaAnnotator.query.filter_by(oa_id=junior_id).delete(synchronize_session=False)
    SeniorJuniorOa.query.filter_by(junior_oa_id=junior_id).delete(synchronize_session=False)

    db.session.delete(user)
    db.session.commit()
    flash(f"Junior OA '{username}' deleted.", "success")
    return redirect(url_for("senior_oa.dashboard"))


@senior_oa_bp.route("/approve_all", methods=["POST"])
@role_required("senior_oa")
def approve_all():
    """Bulk-approve all junior_approved items via streamed batches."""
    from flask import Response, stream_with_context

    links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
    junior_ids = [l.junior_oa_id for l in links]
    if not junior_ids:
        flash("No Junior OAs managed.", "danger")
        return redirect(url_for("senior_oa.dashboard"))

    total = WorkItem.query.filter(
        WorkItem.oa_id.in_(junior_ids),
        WorkItem.status.in_(AWAITING_SENIOR)
    ).count()

    if total == 0:
        flash("No items awaiting final review.", "warning")
        return redirect(url_for("senior_oa.dashboard"))

    BATCH_SIZE = 200

    def generate():
        yield _sse("start", {"total": total})

        bucket = os.environ.get("AWS_S3_BUCKET", "").strip() or None
        now = datetime.now(timezone.utc)
        approved = 0

        while True:
            # Fetch next batch (re-query each loop since we commit each batch)
            items = WorkItem.query.filter(
                WorkItem.oa_id.in_(junior_ids),
                WorkItem.status.in_(AWAITING_SENIOR)
            ).limit(BATCH_SIZE).all()

            if not items:
                break

            for item in items:
                item.status = "approved"
                item.reviewed_at = now

                if bucket:
                    anns = Annotation.query.filter_by(s3_key=item.s3_key).all()
                    lines = [
                        f"{a.class_id} {a.x_center:.6f} {a.y_center:.6f} {a.width:.6f} {a.height:.6f}"
                        for a in anns
                    ]
                    folder = os.path.dirname(item.s3_key)
                    stem = os.path.splitext(os.path.basename(item.s3_key))[0]
                    label_key = f"{folder}/annotated/{stem}.txt" if folder else f"annotated/{stem}.txt"
                    try:
                        put_object(bucket, label_key,
                                   ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                                   content_type="text/plain")
                    except Exception:
                        import traceback; traceback.print_exc()

            db.session.commit()
            approved += len(items)
            yield _sse("progress", {"approved": approved, "total": total})

        yield _sse("done", {"approved": approved})

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(event, data):
    """Format a server-sent event."""
    import json
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _managed_junior_ids():
    links = SeniorJuniorOa.query.filter_by(senior_oa_id=current_user.id).all()
    return [l.junior_oa_id for l in links]


@senior_oa_bp.route("/review")
@role_required("senior_oa")
def review():
    junior_ids = _managed_junior_ids()
    # Optional reviewer filter: scope the queue to one junior OA.
    junior_filter = request.args.get("junior_id", type=int)
    if junior_filter and junior_filter in junior_ids:
        junior_ids = [junior_filter]
    else:
        junior_filter = None

    item = (
        WorkItem.query.filter(
            WorkItem.oa_id.in_(junior_ids),
            WorkItem.status.in_(AWAITING_SENIOR)
        ).first()
        if junior_ids else None
    )
    image_id = item.id if item else None
    junior_name = User.query.get(junior_filter).username if junior_filter else None
    return render_template("senior_oa/review.html",
        image_id=image_id,
        junior_filter=junior_filter,
        junior_name=junior_name,
        class_names=__import__('flask').current_app.config["CLASS_NAMES"],
    )


@senior_oa_bp.route("/review/<int:image_id>")
@role_required("senior_oa")
def review_image(image_id):
    junior_ids = _managed_junior_ids()
    junior_filter = request.args.get("junior_id", type=int)
    if not (junior_filter and junior_filter in junior_ids):
        junior_filter = None

    item = WorkItem.query.filter(
        WorkItem.id == image_id,
        WorkItem.oa_id.in_(junior_ids)
    ).first()
    if not item:
        flash("Image not accessible.", "danger")
        return redirect(url_for("senior_oa.dashboard"))
    junior_name = User.query.get(junior_filter).username if junior_filter else None
    return render_template("senior_oa/review.html",
        image_id=image_id,
        junior_filter=junior_filter,
        junior_name=junior_name,
        class_names=__import__('flask').current_app.config["CLASS_NAMES"],
    )
