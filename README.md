# Local PDF Annotation Tool

A Flask-based web application for annotating PDF pages with bounding boxes for layout segmentation classes. All data is read from and written to local directories -- no cloud services required.

## Overview

This tool reads PDFs from a local directory, converts each page to an image, allows you to draw bounding boxes and assign them to one of 11 layout classes, and saves the annotations as YOLO format text files.

### Input

- **PDF directory**: A local folder containing PDF files, organized into category subdirectories.

```
pdfs/
├── finance/
│   ├── report1.pdf
│   └── report2.pdf
├── medical/
│   └── document.pdf
└── legal/
    └── contract.pdf
```

Categories are automatically detected as subdirectories of the PDF directory.

### Output

- **Output directory**: YOLO label `.txt` files mirroring the PDF directory structure.

```
output/
├── finance/
│   ├── report1_page_1.txt
│   ├── report1_page_2.txt
│   └── report2_page_1.txt
├── medical/
│   └── document_page_1.txt
└── processed_pdfs.json
```

### Annotation Classes (11 total)

1. Header
2. Footer
3. Title
4. Text
5. Table
6. Figure
7. Caption
8. Equation
9. List Item
10. Page Number
11. Section Header

### YOLO Label Format

Each `.txt` file contains one line per bounding box:

```
<class_id> <x_center> <y_center> <width> <height>
```

All coordinates are normalized to `[0, 1]`.

## Installation

### 1. Create Virtual Environment

```bash
cd s3
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Paths

Edit the `.env` file:

```text
PDF_DIR=/path/to/your/pdfs
OUTPUT_DIR=/path/to/your/output
```

- `PDF_DIR`: Directory containing your PDF files. Subdirectories become selectable categories.
- `OUTPUT_DIR`: Where annotation `.txt` files and the `processed_pdfs.json` log are saved.

## Running the Application

```bash
python app.py
```

Then open your browser to: `http://localhost:8000`

On startup, the app prints the configured paths so you can verify them.

## Usage Workflow

1. **Start the app** and open `http://localhost:8000` in your browser.

2. **Select a category** from the dropdown (categories are auto-detected from subdirectories of `PDF_DIR`).

3. **Load a PDF**: Click **"Load Next PDF"** to load the next unprocessed PDF, or enter a number and click **"Go"** to jump to a specific one.

4. **Annotate pages**:
   - Navigate pages using Previous/Next buttons or arrow keys.
   - **Draw Mode OFF**: Click on existing boxes to remove them.
   - **Draw Mode ON**: Drag to draw rectangles around layout elements.
   - After drawing, select a class from the popup.
   - **Clear Region** removes all annotations overlapping the drawn area.

5. **Save annotations**: Click **"Save Changes"** to write the current page's annotations to disk.

6. **Finish PDF**: Click **"Finish PDF"** when done with all pages. This marks the PDF as processed and loads the next one.

7. **Skip PDF**: Click **"Skip PDF"** to mark a corrupt or unwanted PDF as skipped.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Left / Right | Previous / Next page |
| Home / End | First / Last page |
| S | Save current page & go to next |
| D | Toggle Draw Mode |

## File Structure

```
s3/
├── app.py              # Flask backend
├── templates/
│   └── index.html      # Frontend UI
├── requirements.txt    # Python dependencies
├── .env                # Local path configuration
├── README.md           # This file
└── tmp_pages/          # Cached page images (auto-created, safe to delete)
    └── <pdf_id>/
        ├── page_0001.jpg
        ├── page_0002.jpg
        └── ...
```

## How It Works

### PDF Processing

When you load a PDF:
1. The app reads the PDF from your local `PDF_DIR`.
2. Page 1 is converted to JPEG synchronously (so you see it immediately).
3. Remaining pages are converted in the background.
4. All page images are cached in `tmp_pages/` for fast re-access.

### Annotation Storage

Annotations are saved as YOLO-format `.txt` files in `OUTPUT_DIR`, mirroring the category/folder structure of `PDF_DIR`.

### Processed Tracking

The `processed_pdfs.json` file in `OUTPUT_DIR` tracks:
- `processed`: List of PDF relative paths that have been finished.
- `errors`: Dictionary of PDF relative paths that were skipped, with reasons.

Any PDF in either list is skipped when loading the "next" PDF.

## Troubleshooting

### "No PDFs found"
- Verify `PDF_DIR` in `.env` points to the correct directory.
- Ensure PDFs are inside category subdirectories (e.g., `pdfs/finance/file.pdf`).

### Images not loading
- Check that `tmp_pages/` is writable.
- Verify PyMuPDF is installed correctly (`pip install PyMuPDF`).

### No categories showing
- Categories are subdirectories of `PDF_DIR`. Make sure the directory has at least one subdirectory containing PDFs.
