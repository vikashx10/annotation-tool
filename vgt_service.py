"""
VGT (Visual Grounding Transformer) integration service.

Calls the segmentation API in batches of 20, converts bounding boxes to
YOLO normalized format, and maps VGT DocLayNet class IDs to the annotation
tool's class IDs.
"""

import os
import requests
from models import db, PreAnnotation, WorkItem
from s3_service import get_object_bytes

BATCH_SIZE = 20


def _get_segmentation_api():
    """Get the base URL for the segmentation API."""
    url = os.environ.get("SEGMENTATION_MODAL_DOCS", "").strip()
    if not url:
        return None
    return url.replace("/docs", "")


# ── Class mapping: VGT DocLayNet → Annotation Tool ──────────────────────
# VGT classes (DocLayNet 11):
#   0:Caption, 1:Footnote, 2:Formula, 3:List-item, 4:Page-footer,
#   5:Page-header, 6:Picture, 7:Section-header, 8:Table, 9:Text, 10:Title
#
# Tool classes (14):
#   0:Header, 1:Footer, 2:Title, 3:Text, 4:Table, 5:Figure,
#   6:Caption, 7:Equation, 8:List Item, 9:Page Number,
#   10:Section Header, 11:Key-Value Pair, 12:Signature, 13:Seal

VGT_TO_TOOL_CLASS = {
    0: 6,   # Caption → Caption
    1: 1,   # Footnote → Footer
    2: 7,   # Formula → Equation
    3: 8,   # List-item → List Item
    4: 1,   # Page-footer → Footer
    5: 0,   # Page-header → Header
    6: 5,   # Picture → Figure
    7: 10,  # Section-header → Section Header
    8: 4,   # Table → Table
    9: 3,   # Text → Text
    10: 2,  # Title → Title
}


def call_vgt_api(image_bytes, filename="image.png"):
    """Call VGT segmentation /batch_async endpoint for a single image.

    Returns detection dict with: boxes, scores, classes, image_size
    or None on failure.
    """
    api_base = _get_segmentation_api()
    if not api_base:
        return None

    try:
        files = {"file": (filename, image_bytes)}
        data = {"ocr_data": '{"data": []}'}
        response = requests.post(
            f"{api_base}/batch_async",
            files=files,
            data=data,
            timeout=60,
        )
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and "instances" in result:
            return result["instances"]
        return result
    except Exception as e:
        print(f"[VGT] API error for {filename}: {e}")
        return None


def convert_detections(detections):
    """Convert VGT detections to YOLO normalized format with mapped classes.

    Returns list of dicts: [{class_id, x_center, y_center, width, height, confidence}, ...]
    """
    if not detections:
        return []

    boxes = detections.get("boxes", [])
    scores = detections.get("scores", [])
    classes = detections.get("classes", [])
    image_size = detections.get("image_size", [0, 0])

    # image_size is [height, width]
    img_h = float(image_size[0]) if len(image_size) > 0 else 0
    img_w = float(image_size[1]) if len(image_size) > 1 else 0

    if img_w <= 0 or img_h <= 0 or not boxes:
        return []

    results = []
    for i, box in enumerate(boxes):
        try:
            if isinstance(box, dict):
                left = float(box.get("left", 0))
                top = float(box.get("top", 0))
                w = float(box.get("width", 0))
                h = float(box.get("height", 0))
            elif isinstance(box, (list, tuple)) and len(box) >= 4:
                left, top, w, h = float(box[0]), float(box[1]), float(box[2]), float(box[3])
            else:
                continue

            if w < 2 or h < 2:
                continue

            vgt_class = int(classes[i]) if i < len(classes) else 0
            tool_class = VGT_TO_TOOL_CLASS.get(vgt_class)
            if tool_class is None:
                continue

            score = float(scores[i]) if i < len(scores) else 0.0

            x_center = max(0.0, min(1.0, (left + w / 2) / img_w))
            y_center = max(0.0, min(1.0, (top + h / 2) / img_h))
            norm_w = max(0.0, min(1.0, w / img_w))
            norm_h = max(0.0, min(1.0, h / img_h))

            results.append({
                "class_id": tool_class,
                "x_center": x_center,
                "y_center": y_center,
                "width": norm_w,
                "height": norm_h,
                "confidence": score,
            })
        except (TypeError, ValueError, IndexError):
            continue

    return results


def pre_annotate_batch(items, bucket):
    """Run VGT on a batch of WorkItems and save pre-annotations.

    Calls /batch_async per image since the /batch endpoint is unreliable.
    Returns (success_count, failed_count, total_boxes).
    """
    success = 0
    failed = 0
    total_boxes = 0

    for item in items:
        # Skip if already has pre-annotations
        existing = PreAnnotation.query.filter_by(s3_key=item.s3_key).first()
        if existing:
            success += 1
            continue

        try:
            img_bytes = get_object_bytes(bucket, item.s3_key)
        except Exception as e:
            print(f"[VGT] Failed to download {item.s3_key}: {e}")
            failed += 1
            continue

        detections = call_vgt_api(img_bytes, filename=item.filename)
        if detections is None:
            failed += 1
            continue

        converted = convert_detections(detections)
        if converted:
            for ann in converted:
                db.session.add(PreAnnotation(
                    s3_key=item.s3_key,
                    class_id=ann["class_id"],
                    x_center=ann["x_center"],
                    y_center=ann["y_center"],
                    width=ann["width"],
                    height=ann["height"],
                    confidence=ann["confidence"],
                ))
                total_boxes += 1
        else:
            # Save a placeholder so this image is marked as processed
            db.session.add(PreAnnotation(
                s3_key=item.s3_key,
                class_id=-1,
                x_center=0, y_center=0,
                width=0, height=0,
                confidence=0,
            ))

        success += 1

    db.session.commit()

    return (success, failed, total_boxes)
