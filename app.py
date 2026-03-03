from flask import Flask, render_template, send_file, request, jsonify, make_response
import os
import json
import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict, Tuple, Optional

import fitz  # PyMuPDF
from dotenv import load_dotenv
from PIL import Image

app = Flask(__name__)

# Configuration
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# Temporary cache for PDF page images
TMP_DIR = BASE_DIR / "tmp_pages"
TMP_DIR.mkdir(exist_ok=True)


# ---- Mutable directory configuration ----------------------------------------


class DirConfig:
    """Thread-safe container for directory paths (changeable at runtime)."""

    def __init__(self):
        self._lock = threading.Lock()
        env_pdf = os.getenv("PDF_DIR", "").strip()
        env_out = os.getenv("OUTPUT_DIR", "").strip()
        self._pdf_dir = Path(env_pdf).resolve() if env_pdf else None
        self._output_dir = Path(env_out).resolve() if env_out else None
        self._processed_log: Optional[Path] = None
        env_log = os.getenv("PROCESSED_LOG", "").strip()
        if env_log:
            self._processed_log = Path(env_log).resolve()
        self._ensure_dirs()

    def _ensure_dirs(self):
        if self._pdf_dir:
            try:
                self._pdf_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        if self._output_dir:
            try:
                self._output_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    @property
    def is_configured(self) -> bool:
        with self._lock:
            return self._pdf_dir is not None

    @property
    def pdf_dir(self) -> Path:
        with self._lock:
            if self._pdf_dir is None:
                raise ValueError("PDF folder not configured. Please select a folder first.")
            return self._pdf_dir

    @property
    def output_dir(self) -> Path:
        with self._lock:
            if self._output_dir is None:
                raise ValueError("Output folder not configured. Please select a PDF folder first.")
            return self._output_dir

    @property
    def processed_log(self) -> Path:
        with self._lock:
            if self._processed_log:
                return self._processed_log
            if self._output_dir is None:
                raise ValueError("Output folder not configured.")
            return self._output_dir / "processed_pdfs.json"

    def get(self) -> dict:
        with self._lock:
            return {
                "pdf_dir": str(self._pdf_dir) if self._pdf_dir else "",
                "output_dir": str(self._output_dir) if self._output_dir else "",
                "processed_log": str(self._processed_log or (self._output_dir / "processed_pdfs.json" if self._output_dir else "")),
                "configured": self._pdf_dir is not None,
            }

    def set_pdf_dir(self, path: str):
        p = Path(path).resolve()
        if not p.exists():
            raise ValueError(f"Directory does not exist: {p}")
        if not p.is_dir():
            raise ValueError(f"Not a directory: {p}")
        with self._lock:
            self._pdf_dir = p
            # Default output to a sibling "output" directory
            self._output_dir = p.parent / (p.name + "_output")
            self._processed_log = None
        self._ensure_dirs()

    def set_output_dir(self, path: str):
        p = Path(path).resolve()
        with self._lock:
            self._output_dir = p
            self._processed_log = None
        self._ensure_dirs()


dirs = DirConfig()

# Layout classes (12 total)
CLASS_NAMES = [
    "Header",
    "Footer",
    "Title",
    "Text",
    "Table",
    "Figure",
    "Caption",
    "Equation",
    "List Item",
    "Page Number",
    "Section Header",
    "Key-Value Pair",
]


# ---- Thread-safe application state -------------------------------------------


class AppState:
    """Thread-safe container for mutable application state."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current_pdf_path: Optional[str] = None   # relative path from PDF_DIR
        self.current_pdf_id: Optional[str] = None
        self.current_page_count: int = 0

    def get(self) -> dict:
        with self._lock:
            return {
                "pdf_path": self.current_pdf_path,
                "pdf_id": self.current_pdf_id,
                "page_count": self.current_page_count,
            }

    def set(self, pdf_path: str, pdf_id: str, page_count: int):
        with self._lock:
            self.current_pdf_path = pdf_path
            self.current_pdf_id = pdf_id
            self.current_page_count = page_count


state = AppState()


# ---- TTL Cache ---------------------------------------------------------------


class TTLCache:
    """Simple thread-safe TTL cache."""

    def __init__(self, default_ttl: int = 60):
        self._data: Dict[str, Tuple[float, any]] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                expires, value = self._data[key]
                if time.time() < expires:
                    return value
                del self._data[key]
        return None

    def set(self, key: str, value, ttl: int = None):
        with self._lock:
            self._data[key] = (time.time() + (ttl or self._default_ttl), value)

    def invalidate(self, key: str = None):
        with self._lock:
            if key is None:
                self._data.clear()
            elif key in self._data:
                del self._data[key]


_cache = TTLCache(default_ttl=120)


# ---- Background conversion state ---------------------------------------------

_conversion_lock = threading.Lock()
_conversion_status: Dict[str, Dict] = {}  # pdf_id -> {total, converted, done}
_pdf_bytes_cache: Dict[str, bytes] = {}   # pdf_id -> raw PDF bytes


# ---- Prefetch state ----------------------------------------------------------

_prefetch_lock = threading.Lock()
_prefetch_result: Dict[str, any] = {}  # "result" -> {pdf_path, pdf_id, page_count}
_prefetch_thread: Optional[threading.Thread] = None


# ---- Local file helpers ------------------------------------------------------


def load_processed_log() -> Tuple[set, Dict[str, str]]:
    """Load processed PDFs log from local JSON file (with caching)."""
    cached = _cache.get("processed_log")
    if cached is not None:
        return cached

    try:
        log_path = dirs.processed_log
        if log_path.exists():
            with open(log_path, "r") as f:
                payload = json.load(f)
            result = (set(payload.get("processed", [])), payload.get("errors", {}))
        else:
            result = (set(), {})
    except Exception:
        result = (set(), {})

    _cache.set("processed_log", result)
    return result


def save_processed_log(processed: set, errors: Dict[str, str]) -> None:
    """Save processed PDFs log to local JSON file."""
    log_path = dirs.processed_log
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "processed": sorted(processed),
        "errors": errors,
    }
    with open(log_path, "w") as f:
        json.dump(payload, f, indent=2)

    # Invalidate caches on write
    _cache.invalidate("processed_log")
    _cache.invalidate()


def _list_all_pdfs(category: str = None) -> List[str]:
    """List all PDF files under PDF_DIR (with optional category filter).

    Returns relative paths from PDF_DIR, sorted alphabetically.
    """
    cache_key = f"pdf_list:{category or ''}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    pdf_dir_path = dirs.pdf_dir
    search_dir = pdf_dir_path
    if category:
        search_dir = pdf_dir_path / category

    if not search_dir.exists():
        return []

    pdf_paths = []
    for f in sorted(search_dir.rglob("*.pdf")):
        rel = str(f.relative_to(pdf_dir_path))
        pdf_paths.append(rel)

    # Also catch .PDF extension
    for f in sorted(search_dir.rglob("*.PDF")):
        rel = str(f.relative_to(pdf_dir_path))
        if rel not in pdf_paths:
            pdf_paths.append(rel)

    pdf_paths.sort()
    _cache.set(cache_key, pdf_paths)
    return pdf_paths


def list_unprocessed_pdfs(category: str = None, max_files: int = 1000) -> List[str]:
    """List unprocessed PDF relative paths."""
    processed, errors = load_processed_log()
    all_pdfs = _list_all_pdfs(category=category)

    pdf_paths = []
    for rel in all_pdfs:
        if rel in processed or rel in errors:
            continue
        pdf_paths.append(rel)
        if len(pdf_paths) >= max_files:
            break

    return pdf_paths


def pdf_path_to_id(pdf_rel_path: str) -> str:
    """Convert PDF relative path to a unique ID for caching."""
    return hashlib.md5(pdf_rel_path.encode("utf-8")).hexdigest()


def pdf_dir(pdf_id: str) -> Path:
    """Get directory for cached page images of a PDF."""
    d = TMP_DIR / pdf_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _convert_single_page(pdf_bytes: bytes, page_index: int, out_path: Path) -> None:
    """Convert a single PDF page to JPEG."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page = doc[page_index]
        pix = page.get_pixmap(dpi=100)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img.save(str(out_path), "JPEG", quality=80)


def _background_convert(pdf_id: str, pdf_bytes: bytes, total_pages: int, start_page: int = 1):
    """Background thread: convert remaining pages to JPEG."""
    out = pdf_dir(pdf_id)
    converted_count = start_page
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for i in range(start_page, total_pages):
                img_path = out / f"page_{i+1:04d}.jpg"
                if img_path.exists():
                    converted_count = i + 1
                    with _conversion_lock:
                        status = _conversion_status.get(pdf_id)
                        if status:
                            status["converted"] = converted_count
                    continue
                try:
                    page = doc[i]
                    pix = page.get_pixmap(dpi=100)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    img.save(str(img_path), "JPEG", quality=80)
                    converted_count = i + 1
                except Exception:
                    pass
                with _conversion_lock:
                    status = _conversion_status.get(pdf_id)
                    if status:
                        status["converted"] = converted_count
    finally:
        with _conversion_lock:
            status = _conversion_status.get(pdf_id)
            if status:
                status["done"] = True
                status["converted"] = converted_count
        _pdf_bytes_cache.pop(pdf_id, None)


def cache_pdf_pages(pdf_rel_path: str) -> Tuple[str, int]:
    """Read PDF from local disk, convert page 1 synchronously, start background conversion.

    Returns (pdf_id, page_count) immediately after page 1 is ready.
    """
    pdf_id = pdf_path_to_id(pdf_rel_path)
    out = pdf_dir(pdf_id)
    full_path = dirs.pdf_dir / pdf_rel_path

    # Read PDF from local filesystem
    try:
        pdf_bytes = full_path.read_bytes()
    except Exception as e:
        raise Exception(f"Failed to read PDF: {e}")

    if not pdf_bytes:
        raise Exception("PDF file is empty (0 bytes)")

    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            total_pages = len(doc)
    except Exception as e:
        raise Exception(f"Failed to read PDF (corrupt or invalid): {e}")

    # Check if already fully cached
    existing = list(out.glob("page_*.jpg"))
    if len(existing) >= total_pages:
        with _conversion_lock:
            _conversion_status[pdf_id] = {
                "total": total_pages,
                "converted": total_pages,
                "done": True,
            }
        return pdf_id, total_pages

    # Clear old PNG cache but keep existing JPGs
    for f in list(out.glob("page_*.png")):
        f.unlink()

    # Convert page 1 synchronously
    first_page_path = out / "page_0001.jpg"
    if not first_page_path.exists():
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            page = doc[0]
            pix = page.get_pixmap(dpi=100)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img.save(str(first_page_path), "JPEG", quality=80)

    # Set up conversion status
    with _conversion_lock:
        _conversion_status[pdf_id] = {
            "total": total_pages,
            "converted": 1,
            "done": total_pages == 1,
        }

    if total_pages > 1:
        _pdf_bytes_cache[pdf_id] = pdf_bytes
        t = threading.Thread(
            target=_background_convert,
            args=(pdf_id, pdf_bytes, total_pages, 1),
            daemon=True,
        )
        t.start()

    return pdf_id, total_pages


def _ensure_page_converted(pdf_id: str, page_index: int) -> Optional[Path]:
    """On-demand conversion if user skips ahead. Returns path to JPEG."""
    out = pdf_dir(pdf_id)
    img_path = out / f"page_{page_index+1:04d}.jpg"
    if img_path.exists():
        return img_path

    # Try in-memory bytes first
    pdf_bytes = _pdf_bytes_cache.get(pdf_id)

    if pdf_bytes is None:
        # Re-read from local filesystem
        s = state.get()
        if not s["pdf_path"]:
            return None
        try:
            full_path = dirs.pdf_dir / s["pdf_path"]
            pdf_bytes = full_path.read_bytes()
        except Exception:
            return None

    if pdf_bytes is None:
        return None

    try:
        _convert_single_page(pdf_bytes, page_index, img_path)
    except Exception:
        return None
    return img_path


def _label_file_path(pdf_rel_path: str, page_index: int) -> Path:
    """Get local file path for YOLO label file of a page.

    Output structure mirrors the PDF directory structure under OUTPUT_DIR.
    E.g., pdfs/finance/report.pdf page 3 -> output/finance/report_page_3.txt
    """
    p = Path(pdf_rel_path)
    base = p.stem
    parent = p.parent
    return dirs.output_dir / parent / f"{base}_page_{page_index+1}.txt"


def load_yolo_labels(pdf_rel_path: str, page_index: int) -> List[Dict]:
    """Load YOLO labels from local file for a page."""
    label_path = _label_file_path(pdf_rel_path, page_index)
    try:
        if not label_path.exists():
            return []
        content = label_path.read_text().strip()
    except Exception:
        return []

    boxes = []
    for line in content.splitlines():
        parts = line.strip().split()
        if len(parts) != 5:
            continue
        try:
            boxes.append({
                "class_id": int(parts[0]),
                "x_center": float(parts[1]),
                "y_center": float(parts[2]),
                "width": float(parts[3]),
                "height": float(parts[4]),
            })
        except ValueError:
            continue
    return boxes


def save_yolo_labels(pdf_rel_path: str, page_index: int, boxes: List[Dict]) -> None:
    """Save YOLO labels to local file for a page."""
    label_path = _label_file_path(pdf_rel_path, page_index)
    label_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    for box in boxes:
        lines.append(
            f"{box['class_id']} {box['x_center']:.6f} {box['y_center']:.6f} "
            f"{box['width']:.6f} {box['height']:.6f}"
        )
    label_path.write_text("\n".join(lines) + ("\n" if lines else ""))


# ---- Prefetch helpers --------------------------------------------------------


def _do_prefetch(category: str, current_path: str):
    """Background thread: prefetch the next PDF."""
    try:
        candidates = list_unprocessed_pdfs(category=category, max_files=1000)
        if not candidates:
            with _prefetch_lock:
                _prefetch_result.clear()
            return

        # Reorder so we start after current_path
        if current_path:
            after = [k for k in candidates if k > current_path]
            before = [k for k in candidates if k < current_path]
            candidates = after + before

        for pdf_path in candidates:
            try:
                pdf_id, page_count = cache_pdf_pages(pdf_path)
                with _prefetch_lock:
                    _prefetch_result["result"] = {
                        "pdf_path": pdf_path,
                        "pdf_id": pdf_id,
                        "page_count": page_count,
                    }
                return
            except Exception:
                continue

        with _prefetch_lock:
            _prefetch_result.clear()
    except Exception:
        with _prefetch_lock:
            _prefetch_result.clear()


def start_prefetch(category: str, current_path: str):
    """Start prefetching the next PDF in the background."""
    global _prefetch_thread
    with _prefetch_lock:
        _prefetch_result.clear()
    _prefetch_thread = threading.Thread(
        target=_do_prefetch,
        args=(category, current_path),
        daemon=True,
    )
    _prefetch_thread.start()


# ---- Flask routes ------------------------------------------------------------


@app.route("/")
def index():
    """Main page."""
    return render_template("index.html", class_names=CLASS_NAMES)


@app.route("/api/next_pdf", methods=["POST"])
def api_next_pdf():
    """Load next unprocessed PDF."""
    if not dirs.is_configured:
        return jsonify({"status": "error", "message": "Please select a PDF folder first (use the folder bar at the top)"}), 400
    data = request.get_json(silent=True) or {}
    category = data.get("category")
    if not category:
        return jsonify({"status": "error", "message": "Please select a category first"}), 400

    # Check if prefetch has a result ready
    prefetch_hit = None
    with _prefetch_lock:
        if "result" in _prefetch_result:
            prefetch_hit = _prefetch_result.pop("result")

    if prefetch_hit:
        current = state.get()
        if prefetch_hit["pdf_path"] != current["pdf_path"]:
            state.set(prefetch_hit["pdf_path"], prefetch_hit["pdf_id"], prefetch_hit["page_count"])
            start_prefetch(category, prefetch_hit["pdf_path"])
            return jsonify({
                "status": "ok",
                "pdf_path": prefetch_hit["pdf_path"],
                "pdf_id": prefetch_hit["pdf_id"],
                "page_count": prefetch_hit["page_count"],
            })

    candidates = list_unprocessed_pdfs(category=category, max_files=1000)
    if not candidates:
        return jsonify({"status": "empty", "message": "No unprocessed PDFs found"})

    current = state.get()["pdf_path"]
    if current:
        after = [k for k in candidates if k > current]
        before = [k for k in candidates if k < current]
        candidates = after + before

    pdf_id = None
    page_count = 0
    skipped = []
    pdf_path = None
    for pdf_path in candidates:
        try:
            pdf_id, page_count = cache_pdf_pages(pdf_path)
            break
        except Exception as e:
            processed, errors = load_processed_log()
            errors[pdf_path] = str(e)
            processed.add(pdf_path)
            save_processed_log(processed, errors)
            skipped.append(Path(pdf_path).name)

    if pdf_id is None:
        return jsonify({"status": "empty", "message": f"No valid PDFs found (skipped {len(skipped)} bad files)"})

    state.set(pdf_path, pdf_id, page_count)
    start_prefetch(category, pdf_path)

    return jsonify({
        "status": "ok",
        "pdf_path": pdf_path,
        "pdf_id": pdf_id,
        "page_count": page_count,
    })


@app.route("/api/current_pdf")
def api_current_pdf():
    """Get current PDF metadata."""
    return jsonify(state.get())


@app.route("/api/images")
def api_images():
    """Get list of page images for current PDF."""
    s = state.get()
    if not s["pdf_id"]:
        return jsonify([])

    with _conversion_lock:
        status = _conversion_status.get(s["pdf_id"])
    if status:
        return jsonify([f"page_{i+1:04d}" for i in range(status["total"])])

    # Fallback: list actual files on disk
    files = sorted(pdf_dir(s["pdf_id"]).glob("page_*.jpg"))
    return jsonify([f.stem for f in files])


@app.route("/api/image/<image_name>")
def api_image(image_name):
    """Serve page image from cache (with HTTP caching)."""
    s = state.get()
    if not s["pdf_id"]:
        return "No PDF loaded", 400

    img_path = pdf_dir(s["pdf_id"]) / f"{image_name}.jpg"

    if not img_path.exists():
        try:
            page_num = int(image_name.split("_")[-1])
            page_index = page_num - 1
        except (ValueError, IndexError):
            return "Invalid image name", 400

        result = _ensure_page_converted(s["pdf_id"], page_index)
        if result is None or not result.exists():
            return "Image not yet available", 404
        img_path = result

    response = make_response(send_file(str(img_path), mimetype="image/jpeg"))
    response.headers["Cache-Control"] = "public, max-age=3600, immutable"
    return response


@app.route("/api/annotations/<image_name>")
def api_get_annotations(image_name):
    """Get YOLO annotations for a page."""
    s = state.get()
    if not s["pdf_path"]:
        return jsonify([])

    try:
        page_num = int(image_name.split("_")[-1])
        page_index = page_num - 1
    except (ValueError, IndexError):
        return jsonify([])

    return jsonify(load_yolo_labels(s["pdf_path"], page_index))


@app.route("/api/all_annotations")
def api_all_annotations():
    """Get annotations for ALL pages in one batch request."""
    s = state.get()
    if not s["pdf_path"] or not s["page_count"]:
        return jsonify({})

    pdf_path = s["pdf_path"]
    page_count = s["page_count"]

    result = {}

    def fetch_one(i):
        page_name = f"page_{i+1:04d}"
        labels = load_yolo_labels(pdf_path, i)
        return page_name, labels

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_one, i) for i in range(page_count)]
        for future in futures:
            page_name, labels = future.result()
            result[page_name] = labels

    return jsonify(result)


@app.route("/api/annotations/<image_name>", methods=["POST"])
def api_update_annotations(image_name):
    """Save YOLO annotations for a page."""
    s = state.get()
    if not s["pdf_path"]:
        return jsonify({"status": "error", "message": "No PDF loaded"}), 400

    data = request.get_json() or {}
    boxes = data.get("annotations", [])

    try:
        page_num = int(image_name.split("_")[-1])
        page_index = page_num - 1
    except (ValueError, IndexError):
        return jsonify({"status": "error", "message": "Invalid image name"}), 400

    save_yolo_labels(s["pdf_path"], page_index, boxes)
    return jsonify({"status": "success"})


@app.route("/api/conversion_status")
def api_conversion_status():
    """Get background conversion progress for current PDF."""
    s = state.get()
    if not s["pdf_id"]:
        return jsonify({"total": 0, "converted": 0, "done": True})

    with _conversion_lock:
        status = _conversion_status.get(s["pdf_id"])
    if status is None:
        return jsonify({"total": s["page_count"], "converted": s["page_count"], "done": True})

    return jsonify(status)


@app.route("/api/finish_pdf", methods=["POST"])
def api_finish_pdf():
    """Mark current PDF as processed."""
    s = state.get()
    if not s["pdf_path"]:
        return jsonify({"status": "error", "message": "No PDF loaded"}), 400

    processed, errors = load_processed_log()
    processed.add(s["pdf_path"])
    save_processed_log(processed, errors)

    return jsonify({"status": "success"})


@app.route("/api/skip_pdf", methods=["POST"])
def api_skip_pdf():
    """Mark current PDF as error/skip."""
    s = state.get()
    if not s["pdf_path"]:
        return jsonify({"status": "error", "message": "No PDF loaded"}), 400

    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Manually skipped by user")

    processed, errors = load_processed_log()
    errors[s["pdf_path"]] = reason
    processed.add(s["pdf_path"])
    save_processed_log(processed, errors)

    return jsonify({"status": "success"})


@app.route("/api/categories")
def api_categories():
    """Get list of available categories (subdirectories of PDF_DIR)."""
    if not dirs.is_configured:
        return jsonify([])
    pdf_dir_path = dirs.pdf_dir
    if not pdf_dir_path.exists():
        return jsonify([])

    categories = sorted([
        d.name for d in pdf_dir_path.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ])
    return jsonify(categories)


@app.route("/api/load_pdf_by_number", methods=["POST"])
def api_load_pdf_by_number():
    """Load a specific PDF by its 1-based index in the full list for a category."""
    if not dirs.is_configured:
        return jsonify({"status": "error", "message": "Please select a PDF folder first"}), 400
    data = request.get_json(silent=True) or {}
    category = data.get("category")
    number = data.get("number", 1)

    all_pdfs = _list_all_pdfs(category=category)

    if not all_pdfs:
        return jsonify({"status": "empty", "message": "No PDFs found in category"})

    idx = number - 1
    if idx < 0 or idx >= len(all_pdfs):
        return jsonify({"status": "error", "message": f"Number must be between 1 and {len(all_pdfs)}"})

    pdf_path = all_pdfs[idx]
    try:
        pdf_id, page_count = cache_pdf_pages(pdf_path)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

    state.set(pdf_path, pdf_id, page_count)
    start_prefetch(category, pdf_path)

    return jsonify({
        "status": "ok",
        "pdf_path": pdf_path,
        "pdf_id": pdf_id,
        "page_count": page_count,
        "number": number,
        "total": len(all_pdfs),
    })


@app.route("/api/pdf_counts")
def api_pdf_counts():
    """Get total and remaining PDF counts, filtered by category."""
    if not dirs.is_configured:
        return jsonify({"total": 0, "remaining": 0, "processed": 0})
    category = request.args.get("category", "")
    if not category:
        return jsonify({"total": 0, "remaining": 0, "processed": 0})

    cache_key = f"pdf_counts:{category}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    processed, errors = load_processed_log()
    done = processed | set(errors.keys())

    all_pdfs = _list_all_pdfs(category=category)
    total = len(all_pdfs)
    remaining = sum(1 for k in all_pdfs if k not in done)

    result = {"total": total, "remaining": remaining, "processed": total - remaining}
    _cache.set(cache_key, result)
    return jsonify(result)


@app.route("/api/folder")
def api_get_folder():
    """Get current folder configuration."""
    return jsonify(dirs.get())


@app.route("/api/folder", methods=["POST"])
def api_set_folder():
    """Set the PDF source folder (and optionally the output folder)."""
    data = request.get_json(silent=True) or {}
    pdf_dir_path = data.get("pdf_dir", "").strip()
    output_dir_path = data.get("output_dir", "").strip()

    if not pdf_dir_path:
        return jsonify({"status": "error", "message": "pdf_dir is required"}), 400

    try:
        dirs.set_pdf_dir(pdf_dir_path)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    if output_dir_path:
        try:
            dirs.set_output_dir(output_dir_path)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Bad output dir: {e}"}), 400

    # Clear all caches and reset state when folder changes
    _cache.invalidate()
    state.set(None, None, 0)

    return jsonify({"status": "ok", **dirs.get()})


@app.route("/api/browse")
def api_browse():
    """Browse directories on the local filesystem.

    Query params:
        path  – directory to list (default: user home)
    Returns { current, parent, dirs: [...] }
    """
    raw = request.args.get("path", "").strip()
    if not raw:
        raw = str(Path.home())

    target = Path(raw).resolve()
    if not target.exists() or not target.is_dir():
        return jsonify({"status": "error", "message": f"Not a valid directory: {target}"}), 400

    subdirs = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                subdirs.append(entry.name)
    except PermissionError:
        return jsonify({"status": "error", "message": f"Permission denied: {target}"}), 403

    return jsonify({
        "current": str(target),
        "parent": str(target.parent) if target.parent != target else None,
        "dirs": subdirs,
    })


if __name__ == "__main__":
    cfg = dirs.get()
    if cfg["configured"]:
        print(f"PDF directory:    {cfg['pdf_dir']}")
        print(f"Output directory: {cfg['output_dir']}")
    else:
        print("No PDF folder configured. Select one from the UI.")
    print(f"Starting server at http://localhost:8000")
    app.run(debug=True, port=8000, host="0.0.0.0")
