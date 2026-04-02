#!/usr/bin/env python3
"""
Convert PDF pages to images, run VGT segmentation API, render bboxes, and save results.
"""

import os
import sys
import json
import io
from pathlib import Path

import fitz  # PyMuPDF
import requests
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

SEGMENTATION_API = os.getenv("SEGMENTATION_MODAL_DOCS", "").replace("/docs", "")

CLASS_NAMES = {
    0: "Caption",
    1: "Footnote",
    2: "Formula",
    3: "List-item",
    4: "Page-footer",
    5: "Page-header",
    6: "Picture",
    7: "Section-header",
    8: "Table",
    9: "Text",
    10: "Title",
}

# Colors per class (RGB)
CLASS_COLORS = {
    0: (255, 165, 0),    # Caption - orange
    1: (128, 128, 128),  # Footnote - gray
    2: (148, 0, 211),    # Formula - violet
    3: (0, 191, 255),    # List-item - deep sky blue
    4: (169, 169, 169),  # Page-footer - dark gray
    5: (192, 192, 192),  # Page-header - silver
    6: (0, 128, 0),      # Picture - green
    7: (255, 0, 0),      # Section-header - red
    8: (0, 0, 255),      # Table - blue
    9: (0, 0, 0),        # Text - black
    10: (255, 215, 0),   # Title - gold
}


def pdf_to_images(pdf_path, dpi=200):
    """Convert each PDF page to a PIL Image."""
    doc = fitz.open(pdf_path)
    images = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for page_num in range(len(doc)):
        pix = doc[page_num].get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        images.append((page_num + 1, img))
    doc.close()
    return images


def call_segmentation_api(image_bytes, filename="page.png"):
    """Call the VGT segmentation API."""
    try:
        files = {"file": (filename, image_bytes)}
        data = {"ocr_data": '{"data": []}'}
        response = requests.post(
            f"{SEGMENTATION_API}/batch_async",
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
        print(f"  Error calling API for {filename}: {e}")
        return None


def render_bboxes(image, detections):
    """Draw bounding boxes on the image and return it."""
    if not detections:
        return image

    draw = ImageDraw.Draw(image)

    boxes = detections.get("boxes", [])
    scores = detections.get("scores", [])
    classes = detections.get("classes", [])

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

            class_id = int(classes[i]) if i < len(classes) else 0
            score = float(scores[i]) if i < len(scores) else 0.0
            class_name = CLASS_NAMES.get(class_id, f"class_{class_id}")
            color = CLASS_COLORS.get(class_id, (255, 0, 0))

            x1, y1 = left, top
            x2, y2 = left + w, top + h

            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

            label = f"{class_name} {score:.2f}"
            # Draw label background
            bbox_text = draw.textbbox((x1, y1 - 14), label)
            draw.rectangle([bbox_text[0] - 1, bbox_text[1] - 1, bbox_text[2] + 1, bbox_text[3] + 1], fill=color)
            draw.text((x1, y1 - 14), label, fill="white")

        except (TypeError, ValueError, IndexError):
            continue

    return image


def save_detections_json(detections, output_path):
    """Save detections as structured JSON."""
    if not detections:
        data = {"detections": []}
    else:
        boxes = detections.get("boxes", [])
        scores = detections.get("scores", [])
        classes = detections.get("classes", [])
        image_size = detections.get("image_size", [0, 0])

        detection_list = []
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

                score = float(scores[i]) if i < len(scores) else 0
                class_id = int(classes[i]) if i < len(classes) else 0
                class_name = CLASS_NAMES.get(class_id, f"class_{class_id}")

                detection_list.append({
                    "bbox": {"left": round(left, 2), "top": round(top, 2), "width": round(w, 2), "height": round(h, 2)},
                    "class_id": class_id,
                    "class_name": class_name,
                    "confidence": round(score, 4),
                })
            except (TypeError, ValueError, IndexError):
                continue

        data = {"image_size": image_size, "detections": detection_list}

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_vgt_pdf.py <pdf_path>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    # Output directory
    output_dir = Path(__file__).parent / "results" / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Processing: {pdf_path}")
    print(f"Output dir: {output_dir}")
    print(f"API: {SEGMENTATION_API}")

    # Convert PDF to images
    print("\nConverting PDF pages to images...")
    pages = pdf_to_images(pdf_path)
    print(f"Total pages: {len(pages)}")

    all_results = {}

    for page_num, img in pages:
        print(f"\n--- Page {page_num}/{len(pages)} ---")

        # Convert PIL image to PNG bytes for API
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_bytes = buf.getvalue()

        # Save raw page image
        raw_path = output_dir / f"page_{page_num:03d}.png"
        img.save(raw_path)
        print(f"  Saved page image: {raw_path.name}")

        # Call API
        print(f"  Calling segmentation API...")
        detections = call_segmentation_api(img_bytes, filename=f"page_{page_num}.png")

        if detections:
            num_boxes = len(detections.get("boxes", []))
            print(f"  Got {num_boxes} detections")

            # Save JSON
            json_path = output_dir / f"page_{page_num:03d}_detections.json"
            save_detections_json(detections, json_path)
            print(f"  Saved JSON: {json_path.name}")

            # Render bboxes on image
            rendered = render_bboxes(img.copy(), detections)
            render_path = output_dir / f"page_{page_num:03d}_rendered.png"
            rendered.save(render_path)
            print(f"  Saved rendered: {render_path.name}")

            all_results[f"page_{page_num}"] = {
                "num_detections": num_boxes,
                "json_file": str(json_path.name),
                "rendered_file": str(render_path.name),
            }
        else:
            print(f"  No detections (API returned None)")
            all_results[f"page_{page_num}"] = {"num_detections": 0}

    # Save summary
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump({"pdf": str(pdf_path), "total_pages": len(pages), "pages": all_results}, f, indent=2)
    print(f"\nSummary saved to: {summary_path}")
    print("Done!")


if __name__ == "__main__":
    main()
