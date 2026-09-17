"""
Annotation validation run when a Junior OA approves a page.

Pure functions — no Flask, no DB. Boxes are the YOLO-normalized dicts the
canvas sends ({class_id, x_center, y_center, width, height}); box_indexes in
the returned issues refer to positions in that list so the UI can highlight them.

Rules:
  duplicate   — two boxes on (almost) the same region
  overlap     — two boxes covering part of the same region / one nested in another
  empty_page  — no boxes on a page that has content
"""

import io
from PIL import Image

# ── Thresholds ───────────────────────────────────────────────────────────
# Two boxes with IoU above this are the same region drawn twice.
DUPLICATE_IOU = 0.90
# Overlap is flagged when the shared area exceeds this share of the SMALLER box.
OVERLAP_MIN_RATIO = 0.10
# ...and the smaller box is "nested" when this much of it lies inside the other.
NESTED_MIN_RATIO = 0.90
# Shared strips thinner than this (normalized, ~3px on a 1000px page) are just
# neighbouring boxes touching at the edge — not an overlap.
TOUCH_TOLERANCE = 0.003

# Empty-page check: the page is analysed at this size...
INK_ANALYSIS_DIM = 500
# ...ignoring this share of each side (scan shadows / punch holes live there)...
INK_BORDER_MARGIN = 0.02
# ...a pixel is "ink" when it differs from the page background by this many levels...
INK_LEVEL_DELTA = 60
# ...and the page has content when ink exceeds this share of it (~one line of text).
BLANK_PAGE_MAX_INK = 0.001

MAX_ISSUES = 50


def _corners(box):
    """YOLO center/size → (x1, y1, x2, y2). Raises ValueError on malformed boxes."""
    try:
        xc, yc = float(box["x_center"]), float(box["y_center"])
        w, h = float(box["width"]), float(box["height"])
        int(box["class_id"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Malformed annotation box")
    return (xc - w / 2, yc - h / 2, xc + w / 2, yc + h / 2)


def _area(c):
    return max(0.0, c[2] - c[0]) * max(0.0, c[3] - c[1])


def _class_name(box, class_names):
    cid = int(box["class_id"])
    return class_names[cid] if 0 <= cid < len(class_names) else f"Class {cid}"


def _issue(rule, message, box_indexes=()):
    return {"rule": rule, "severity": "error", "message": message,
            "box_indexes": list(box_indexes)}


def check_box_pairs(boxes, class_names=()):
    """Duplicate + overlap rules. One issue per offending pair."""
    corners = [_corners(b) for b in boxes]
    issues = []

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = corners[i], corners[j]
            iw = min(a[2], b[2]) - max(a[0], b[0])
            ih = min(a[3], b[3]) - max(a[1], b[1])
            if iw <= TOUCH_TOLERANCE or ih <= TOUCH_TOLERANCE:
                continue

            inter = iw * ih
            area_a, area_b = _area(a), _area(b)
            smaller = min(area_a, area_b)
            if smaller <= 0:
                continue

            name_i, name_j = _class_name(boxes[i], class_names), _class_name(boxes[j], class_names)
            label = f"#{i + 1} {name_i} and #{j + 1} {name_j}"
            iou = inter / (area_a + area_b - inter)

            if iou >= DUPLICATE_IOU:
                if int(boxes[i]["class_id"]) == int(boxes[j]["class_id"]):
                    msg = f"Duplicate box: {label} cover the same region. Remove one."
                else:
                    msg = f"Conflicting labels: {label} cover the same region. Keep one."
                issues.append(_issue("duplicate", msg, (i, j)))
            elif inter / smaller >= NESTED_MIN_RATIO:
                inner, outer = (i, j) if area_a <= area_b else (j, i)
                msg = (f"Nested box: #{inner + 1} {_class_name(boxes[inner], class_names)} is inside "
                       f"#{outer + 1} {_class_name(boxes[outer], class_names)}.")
                issues.append(_issue("overlap", msg, (i, j)))
            elif inter / smaller >= OVERLAP_MIN_RATIO:
                pct = round(100 * inter / smaller)
                issues.append(_issue("overlap", f"Overlap: {label} overlap ({pct}% of the smaller box).", (i, j)))

            if len(issues) >= MAX_ISSUES:
                return issues
    return issues


def ink_ratio(image_bytes):
    """Share of the page that is not background (0.0–1.0).

    Background is the most common grey level, so this works for grey scans and
    dark slides alike. Histogram-only — no per-pixel Python loops.
    """
    with Image.open(io.BytesIO(image_bytes)) as im:
        if im.mode in ("P", "RGBA", "LA"):
            # Flatten transparency onto white, otherwise it reads as black ink.
            rgba = im.convert("RGBA")
            flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            flat.alpha_composite(rgba)
            im = flat
        im = im.convert("L")
        im.thumbnail((INK_ANALYSIS_DIM, INK_ANALYSIS_DIM), Image.BOX)
        w, h = im.size
        mx, my = int(w * INK_BORDER_MARGIN), int(h * INK_BORDER_MARGIN)
        if w - 2 * mx > 0 and h - 2 * my > 0:
            im = im.crop((mx, my, w - mx, h - my))
        hist = im.histogram()

    total = sum(hist)
    if not total:
        return 0.0
    # Smooth over ±2 levels so JPEG noise doesn't split the background peak.
    smoothed = [sum(hist[max(0, i - 2):i + 3]) for i in range(256)]
    background = max(range(256), key=smoothed.__getitem__)
    ink = sum(n for level, n in enumerate(hist) if abs(level - background) > INK_LEVEL_DELTA)
    return ink / total


def check_empty_page(boxes, load_image_bytes):
    """No boxes is only valid on a blank page. The image is loaded only when needed."""
    if boxes:
        return []
    try:
        ratio = ink_ratio(load_image_bytes())
    except Exception as e:
        print(f"[validation] could not read page for empty-page check: {e}")
        return [_issue("empty_page", "Could not load the page to confirm it is blank. Try again.")]
    if ratio > BLANK_PAGE_MAX_INK:
        return [_issue("empty_page", "No boxes, but the page has content. Annotate it or reject it.")]
    return []


def validate(boxes, load_image_bytes, class_names=()):
    """Run all rules. Returns a list of issues — empty means the page may be approved.

    load_image_bytes: zero-arg callable returning the page image bytes; only
    called by rules that need pixels.
    Raises ValueError if a box is malformed.
    """
    return check_empty_page(boxes, load_image_bytes) + check_box_pairs(boxes, class_names)
