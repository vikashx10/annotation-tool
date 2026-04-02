# Changes — VGT Pre-Annotation Integration

## New Files

| File | Purpose |
|------|---------|
| `vgt_service.py` | VGT segmentation API service — calls `/batch_async` endpoint, converts detections to YOLO normalized format, maps DocLayNet class IDs to annotation tool class IDs |
| `vgt.py` | Standalone script to download S3 images, run segmentation, and save detection JSONs |
| `run_vgt_pdf.py` | Standalone script to run VGT on a local PDF — converts pages to images, calls segmentation API, renders bboxes, saves results to `results/` folder |

## Modified Files

### `models.py`
- Added `PreAnnotation` model (`pre_annotations` table) — stores VGT predictions per S3 key with `class_id`, `x_center`, `y_center`, `width`, `height`, `confidence`. Uses `class_id = -1` as placeholder for images processed with zero detections.

### `routes_admin.py`
- **Pre-annotation count**: Dashboard now counts how many S3 keys under each OA's prefixes already have pre-annotations (using `LIKE prefix%` query). Shows remaining count on the button.
- **Pre-Annotate button**: Shows "Pre-Annotate (N)" where N = total cursor images minus already pre-annotated. Shows "counting..." while S3 count loads, "VGT done" when all processed.
- **Background job**: `_run_pre_annotate()` iterates all S3 images in OA's prefixes, skips already pre-annotated keys, calls VGT API per image via `/batch_async` (the `/batch` endpoint was returning empty results).
- **Stop support**: Added `stop` flag in job dict, checked each iteration. New route `POST /admin/pre_annotate_stop/<oa_id>` sets the flag. Thread exits gracefully with `status: "stopped"`.
- **Job status persistence**: `_pre_annotate_jobs` dict passed to template so stopped/done/error results survive page reload.
- **Routes added**:
  - `POST /admin/pre_annotate/<oa_id>` — start VGT pre-annotation
  - `GET /admin/pre_annotate_status/<oa_id>` — poll progress
  - `POST /admin/pre_annotate_stop/<oa_id>` — stop running job

### `routes_api.py`
- `GET /api/annotations/<image_id>` — now falls back to `PreAnnotation` records when no manual annotations exist and item is pending/rejected. Filters out placeholder rows (`class_id >= 0`). Returns `"source": "pre_annotation"` or `"source": "annotation"` to distinguish.

### `templates/admin/dashboard.html`
- **Pre-Annotate button states**: disabled while counting, enabled with remaining count, "VGT done" when complete.
- **Stop button**: Red "Stop" button appears below progress while job is running. Calls stop endpoint, shows "Stopping...".
- **Job result display**: After stop/done/error, shows inline summary below button — processed count, boxes, failed, remaining.
- **Progress display**: Shows "Scanning S3..." while collecting keys, then `processed/total (%)` with live polling.
- **Count polling**: When S3 image count finishes loading, automatically enables the Pre-Annotate button with the correct count.

### `routes_annotator.py`
- Minor: imports `PreAnnotation` for pre-annotation awareness in annotator views.

## Bug Fixes

1. **`/batch` endpoint returning empty results**: The VGT API's `/batch` endpoint always returned `[]`. Switched to `/batch_async` (single image endpoint) which works correctly.
2. **Zero-detection images not tracked**: Images processed by VGT with 0 valid detections had no `PreAnnotation` row saved, so they kept appearing as "not pre-annotated". Fixed by saving a placeholder row (`class_id = -1`).
3. **Button count not updating after stop**: Page reload lost job data and showed full total again. Fixed by passing job dict to template and rendering last result server-side.
