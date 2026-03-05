import os
from pathlib import Path
from models import db, ImageRecord

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def scan_images(root_path):
    """Walk directory tree, find all images, insert new ones into DB.

    Returns count of newly added images.
    """
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Invalid image root: {root}")

    added = 0
    for dirpath, _, filenames in os.walk(root):
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                continue
            full = Path(dirpath) / fname
            rel = str(full.relative_to(root))
            existing = ImageRecord.query.filter_by(relative_path=rel).first()
            if not existing:
                db.session.add(ImageRecord(relative_path=rel, filename=fname))
                added += 1

    db.session.commit()
    return added
