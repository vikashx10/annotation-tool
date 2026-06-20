"""
Gemini model-review service (via OpenRouter).

A SEPARATE review path that runs in parallel to the human Junior OA review.
It takes WorkItems still "awaiting junior review" (status == "annotated") and asks
Gemini to RE-ANNOTATE the document — i.e. produce its own corrected set of
bounding boxes for the layout. This is ANNOTATION only (not a review step):

  - the item's annotations are replaced with Gemini's boxes,
  - the label is written to a SEPARATE S3 directory (MODEL_LABEL_DIR),
  - the item STAYS "annotated" so it flows through the normal human Junior
    Review like any other annotated image (marked via model_note).

The prompt / bbox format / class enum / retry + sanitization logic mirror the
production Rust segmentation service. If OPENROUTER_API_KEY is not configured
every call returns None and the batch is a graceful no-op.
"""

import os
import io
import re
import json
import time
import base64
import random
import requests
from PIL import Image
from models import db, Annotation
from s3_service import get_object_bytes, put_object

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Sub-directory (next to each image) where Gemini's re-annotation labels are
# written — kept separate from the human "annotated/" outputs.
MODEL_LABEL_DIR = os.environ.get("MODEL_LABEL_DIR", "model_annotated").strip() or "model_annotated"

# The model outputs THESE class names directly — they are EXACTLY this tool's
# CLASS_NAMES (app.py), so there is no lossy name mapping. The stored class id is
# resolved from the matching CLASS_NAMES index at call time. Keep this list in
# sync with CLASS_NAMES.
GEMINI_CLASSES = [
    "Header", "Footer", "Title", "Text", "Table", "Figure",
    "Caption", "Equation", "List Item", "Page Number",
    "Section Header", "Key-Value Pair", "Signature", "Seal",
]


def _get_api_key():
    # OPENROUTER_API_KEY preferred; GEMINI_API_KEY kept as a fallback name.
    return (os.environ.get("OPENROUTER_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or None)


def _get_model():
    return os.environ.get("GEMINI_MODEL", "google/gemini-3.1-pro-preview").strip() \
        or "google/gemini-3.1-pro-preview"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


_PROMPT = """You are a document layout segmentation model that produces TRAINING-GROUND-TRUTH for a
PDF parsing/chunking pipeline. The image is {width} pixels wide and {height} pixels tall.

CORE PRINCIPLE — a label is not "what this looks like", it is "how this should be chunked
downstream". Section headers open a new chunk; key-value pairs become their own atomic units.
Segment for the chunk you want, not just the pixels you see.

Output: for each region return its class and bounding box [y_min, x_min, y_max, x_max] in
0-1000 normalized coordinates ((0,0) = top-left, (1000,1000) = bottom-right).

Classes:
{classes}

Class definitions (with downstream meaning):
- "Title" = the document's main title or form identifier (usually the largest text near the top). Top-level chunk.
- "Section Header" = a heading that OPENS a logical section. Includes numbered/styled section titles
  (e.g. "Part I", "Part II", "Part 1", "1.", "2."), bold/shaded/boxed title bars, standalone titles
  (e.g. "Identification", "Election"), and column captions that label a block (e.g. "Business 1 / Business 2").
- "Key-Value Pair" = ONE label + its value/answer slot — the box covers BOTH, including the
  blank input area where the value goes (e.g. "Business Number: ____", "Name: John", "Date: 01/01/2024").
  A checkbox + its statement is a Key-Value Pair.
- "Text" = free-flowing paragraph or body text.
- "Table" = a TRUE tabular grid of rows x columns (preserved as a table downstream).
- "List Item" = a single bullet or numbered instruction/list entry (one box per bullet).
- "Figure" = images, logos, agency banners, diagrams, charts, decorative/stamp marks (non-text).
- "Caption" = text that describes a figure or table.
- "Equation" = a mathematical formula or equation.
- "Header" = repeated page furniture at the very top (logos strip / form ID banner) that is not the Title.
- "Footer" = repeated page furniture at the very bottom (form code, "Printed in ...", copyright). Boilerplate.
- "Page Number" = a standalone page number (e.g. "3" or "Page 3").
- "Signature" = a signature field or signed area.
- "Seal" = an official stamp, seal, or embossed mark.

THE HIERARCHY RULE (most important):
- A "Section Header" box covers the HEADER TEXT ONLY — never the fields or content beneath it.
- Each field inside a section is its OWN "Key-Value Pair" box. NEVER wrap a whole section
  (header + all its fields) in one big box — that is the #1 mistake.
- Headers and their fields are SEPARATE boxes even though they are logically parent/child;
  the nesting is reconstructed later from position + label.
- Numbered/lettered items ("Part I", "1.", "A", "B", … "X") that introduce content are "Section Header",
  not part of the field below them.

KEY-VALUE-PAIR RULES:
- Treat every form INPUT FIELD as a "Key-Value Pair": the label/question is the key and its
  input area (blank line/cell, checkbox(es), dropdown, date, signature line, etc.) is the value.
  Box the whole field — key + value area — as one region, whatever the value widget is.
- The box MUST enclose BOTH the label AND its value/answer area as one rectangle —
  INCLUDING the empty input space (the blank cell, underline, write-in line, box,
  or whitespace where the value goes). Do NOT box only the label text. This is a
  common mistake: the value slot is usually blank on a form, but it is PART of the
  field — extend the box to the field's visual boundary (the cell border, the end
  of the underline/dotted line, or the input-box outline) even when the value is
  empty/unfilled. For a checkbox item, include both the checkbox and its statement.
- One field (label + its value slot) = one box. Do NOT merge several fields together.
- QUESTION + ANSWER OPTIONS = ONE "Key-Value Pair". When a question/prompt is followed by a
  set of selectable options (a row/group of checkboxes or radio buttons, or a multiple-choice
  list such as years, Yes/No, or A/B/C choices), the QUESTION is the key and ALL its options
  together are the value. Put the question text AND the entire option group inside ONE box.
  Do NOT split each option (e.g. each year checkbox "2017", "2018", …) into its own box, and
  do NOT label the question separately as "Text" or "Section Header".
  • Example: 'Indicate which program year(s): [ ]2017 [ ]2018 [x]2019 [x]2020 [ ]2021' → ONE box.
  Distinguish this from INDEPENDENT checkbox statements, where each checkbox has its own complete
  standalone statement (e.g. "I recommend approval." / "I recommend denial.") — those are one
  "Key-Value Pair" EACH.
- A row with multiple value columns (e.g. "Rents ........ $[__]" with Business 1 / Business 2 columns)
  is still ONE "Key-Value Pair" per row — do not split per column, and do not label it as a Table.
- A field that wraps onto two lines (long question + a Yes/No box on the right) is ONE "Key-Value Pair".
- A From/To date range is one "Key-Value Pair" if it is a single logical field.
- Bilingual labels (e.g. English + French) are ONE region — do not split by language.

PROCEDURE:
1. Scan the whole page first; identify the skeleton (title, headers, sections, footer).
2. Mark page furniture ("Figure" logos, "Footer") first so they are not folded into content.
3. Mark the "Title".
4. Mark every "Section Header" top-to-bottom (numbering, bold/large text, shaded bars, standalone titles).
5. Inside each section, box every "Key-Value Pair" separately (checkboxes included).
6. Mark "List Item"s (each bullet) and "Signature" fields.
7. Review boundaries: no box may swallow a header together with its fields.

COMPLETENESS:
- Every visible element MUST be inside exactly one tight bounding box. Scan top-left to bottom-right
  and do not skip anything. After segmenting, verify nothing visible is left uncovered.
- Boxes must be TIGHT around their region and never cut off text. NOTE: for a
  "Key-Value Pair" the region INCLUDES the value/answer slot, so enclosing that
  blank input area is REQUIRED and is NOT considered loose whitespace.
- Do not overlap regions unless nesting is unavoidable. If unsure of a class, use "Text".

Return ONLY a JSON object of this exact shape (no markdown, no explanation):
{{"boxes": [{{"class": "Title", "bbox": [y_min, x_min, y_max, x_max]}}, ...]}}"""


# JSON schema handed to OpenRouter for structured output (maps to Gemini responseSchema).
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "page_segments",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "boxes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "class": {"type": "string", "enum": GEMINI_CLASSES},
                            "bbox": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 4, "maxItems": 4,
                            },
                        },
                        "required": ["class", "bbox"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["boxes"],
            "additionalProperties": False,
        },
    },
}


def _image_dimensions(image_bytes):
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            return im.size  # (width, height)
    except Exception:
        return (0, 0)


def _build_class_index(class_names):
    """Case-insensitive lookup: tool class name -> id."""
    return {name.strip().lower(): i for i, name in enumerate(class_names)}


def _box2d_to_yolo(bbox):
    """Convert [y_min, x_min, y_max, x_max] (0-1000) -> YOLO normalized center."""
    y_min, x_min, y_max, x_max = (float(v) for v in bbox)
    y_min, y_max = sorted((y_min, y_max))
    x_min, x_max = sorted((x_min, x_max))
    x_center = max(0.0, min(1.0, (x_min + x_max) / 2 / 1000.0))
    y_center = max(0.0, min(1.0, (y_min + y_max) / 2 / 1000.0))
    width = max(0.0, min(1.0, (x_max - x_min) / 1000.0))
    height = max(0.0, min(1.0, (y_max - y_min) / 1000.0))
    return x_center, y_center, width, height


class _RetryableError(Exception):
    """A transient failure (network, rate-limit, 5xx, truncation) worth retrying."""


def _prepare_image(image_bytes, filename):
    """Normalize + optionally downscale the image for the API.

    Returns (jpeg_bytes, width, height). Re-encoding to a bounded JPEG keeps the
    request payload small (avoids size limits / token blow-ups). Bounding boxes
    are 0-1000 normalized, so downscaling does not affect coordinates.
    Falls back to the original bytes on any error.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            # Flatten palette/alpha cleanly (avoids Pillow transparency warnings).
            if im.mode in ("P", "RGBA", "LA"):
                im = im.convert("RGBA")
            im = im.convert("RGB")
            w, h = im.size
            max_dim = _env_int("GEMINI_MAX_IMAGE_DIM", 1600)
            longest = max(w, h)
            if longest > max_dim > 0:
                scale = max_dim / longest
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            sw, sh = im.size
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=90)
            return buf.getvalue(), sw, sh
    except Exception as e:
        print(f"[Gemini] image prep failed for {filename}: {e}")
        w, h = _image_dimensions(image_bytes)
        return image_bytes, w, h


def _parse_boxes_text(text):
    """Best-effort extraction of a list of {class, bbox} dicts from model output.

    Tolerant of code fences, control characters, trailing junk, and TRUNCATED
    JSON (recovers whatever complete box objects are present). Always returns a
    list (empty if nothing parseable).
    """
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = "\n".join(t.split("\n")[1:])
        if "```" in t:
            t = t[:t.rfind("```")]
    # Strip control chars that strict JSON parsers reject.
    t = re.sub(r"[\x00-\x1f]", "", t).strip()

    # 1) Try a clean parse of the object / array, with a couple of trimmed fallbacks.
    candidates = [t]
    if "{" in t and "}" in t:
        candidates.append(t[t.find("{"):t.rfind("}") + 1])
    if "[" in t and "]" in t:
        candidates.append(t[t.find("["):t.rfind("]") + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("boxes"), list):
            return obj["boxes"]
        if isinstance(obj, list):
            return obj

    # 2) Salvage individual flat {...} box objects (handles truncated arrays).
    salvaged = []
    for m in re.finditer(r"\{[^{}]*\}", t):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(obj, dict) and "class" in obj and "bbox" in obj:
            salvaged.append(obj)
    return salvaged


def _call_openrouter(prompt, data_url, api_key, max_tokens):
    """Single OpenRouter call. Returns (boxes_list, truncated_flag).

    Raises _RetryableError for transient issues, or a plain Exception for
    non-retryable client errors.
    """
    payload = {
        "model": _get_model(),
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": _RESPONSE_FORMAT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://annotation-tool.local",
        "X-Title": "Annotation Tool Model Review",
    }
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload,
                             timeout=_env_int("GEMINI_TIMEOUT", 300))
    except requests.RequestException as e:
        raise _RetryableError(f"network error: {e}")

    if resp.status_code == 429 or resp.status_code >= 500:
        raise _RetryableError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        finish = (choice.get("finish_reason") or choice.get("native_finish_reason") or "").lower()
    except Exception as e:
        raise _RetryableError(f"unexpected response shape: {e}")

    truncated = finish in ("length", "max_tokens")
    return _parse_boxes_text(content), truncated


def reannotate_image(image_bytes, filename, class_names):
    """Ask Gemini to annotate one image — robust to transient failures.

    Retries on network errors, rate limits (429), 5xx, and truncation (bumping
    max_tokens), with exponential backoff + jitter. Recovers partial boxes from
    truncated/malformed JSON. Returns a list of boxes
    [{class_id, x_center, y_center, width, height}, ...] (possibly empty) on any
    successful call, or None only when no API key is set or every retry failed.
    """
    api_key = _get_api_key()
    if not api_key:
        return None

    img_bytes, width, height = _prepare_image(image_bytes, filename)
    prompt = _PROMPT.format(
        width=width, height=height,
        classes="\n".join(f"  - {c}" for c in GEMINI_CLASSES),
    )
    data_url = f"data:image/jpeg;base64,{base64.b64encode(img_bytes).decode('ascii')}"

    max_retries = max(1, _env_int("GEMINI_MAX_RETRIES", 5))
    base_tokens = _env_int("GEMINI_MAX_OUTPUT_TOKENS", 16384)

    best = None  # None = no successful response yet; [] = success with 0 boxes
    for attempt in range(max_retries):
        max_tokens = min(base_tokens * (attempt + 1), 65536)
        do_retry = False
        try:
            boxes, truncated = _call_openrouter(prompt, data_url, api_key, max_tokens)
            # Keep the richest box set seen across attempts.
            if best is None or len(boxes) > len(best):
                best = boxes
            do_retry = truncated  # only retry to recover a fuller result
        except _RetryableError as e:
            print(f"[Gemini] attempt {attempt + 1}/{max_retries} retryable for {filename}: {e}")
            do_retry = True
        except Exception as e:
            print(f"[Gemini] attempt {attempt + 1}/{max_retries} fatal for {filename}: {e}")
            break  # non-retryable client error
        if not do_retry or attempt + 1 >= max_retries:
            break
        time.sleep(min(2 ** attempt, 30) + random.uniform(0, 0.5))

    if best is None:
        return None

    raw_boxes = best
    class_index = _build_class_index(class_names)
    boxes = []
    for b in raw_boxes:
        try:
            # Model emits this tool's exact class names — resolve id directly.
            class_id = class_index.get(str(b.get("class", "")).strip().lower())
            if class_id is None:
                continue
            bbox = b.get("bbox") or []
            if len(bbox) != 4:
                continue
            x_center, y_center, w, h = _box2d_to_yolo(bbox)
            if w <= 0 or h <= 0:
                continue
            boxes.append({
                "class_id": class_id,
                "x_center": x_center, "y_center": y_center,
                "width": w, "height": h,
            })
        except (TypeError, ValueError, IndexError):
            continue
    return boxes


def _save_boxes(s3_key, boxes):
    """Replace all annotations for s3_key with Gemini's re-annotation boxes."""
    Annotation.query.filter_by(s3_key=s3_key).delete()
    for b in boxes:
        db.session.add(Annotation(
            s3_key=s3_key,
            class_id=int(b["class_id"]),
            x_center=float(b["x_center"]),
            y_center=float(b["y_center"]),
            width=float(b["width"]),
            height=float(b["height"]),
        ))


def _model_label_key(s3_key):
    """Label path in the SEPARATE model directory: path/<MODEL_LABEL_DIR>/stem.txt"""
    folder = os.path.dirname(s3_key)
    stem = os.path.splitext(os.path.basename(s3_key))[0]
    return f"{folder}/{MODEL_LABEL_DIR}/{stem}.txt" if folder else f"{MODEL_LABEL_DIR}/{stem}.txt"


def _write_model_label(bucket, s3_key, boxes):
    """Write the YOLO label for Gemini's boxes into the separate model directory."""
    if not bucket:
        return
    lines = [
        f"{b['class_id']} {b['x_center']:.6f} {b['y_center']:.6f} {b['width']:.6f} {b['height']:.6f}"
        for b in boxes
    ]
    try:
        put_object(bucket, _model_label_key(s3_key),
                   ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                   content_type="text/plain")
    except Exception:
        import traceback; traceback.print_exc()


def annotate_work_item(item, bucket, class_names):
    """Re-annotate ONE WorkItem in place with Gemini (no commit).

    Replaces its annotations with Gemini's boxes, writes the label to the
    separate model directory, and marks it via model_note. The item STAYS
    'annotated' (normal Junior queue). Returns the box count on success, or
    None on failure (item left untouched). The caller commits.
    """
    if not bucket:
        return None
    try:
        img_bytes = get_object_bytes(bucket, item.s3_key)
    except Exception as e:
        print(f"[Gemini] failed to download {item.s3_key}: {e}")
        return None

    boxes = reannotate_image(img_bytes, item.filename, class_names)
    if boxes is None:
        return None

    _save_boxes(item.s3_key, boxes)
    _write_model_label(bucket, item.s3_key, boxes)
    item.model_note = f"[Gemini] re-annotated: {len(boxes)} box(es)"
    return len(boxes)
