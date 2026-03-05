# Multi-Role Image Annotation Tool

A Flask-based web application for annotating images with bounding boxes for layout segmentation classes. Features role-based access with Admin, OA (Quality Assurer), and Annotator roles. All data stored in SQLite.

## Overview

This tool scans images (JPG/PNG) from a local directory, allows annotators to draw bounding boxes and assign layout classes, OAs to review and approve/reject annotations, and admins to manage users and export YOLO-format labels.

### Roles

- **Admin**: Manage users, set image root directory, view progress, export approved annotations
- **OA (Quality Assurer)**: Manage annotators, distribute images, review annotations (approve/reject)
- **Annotator**: Draw bounding boxes on assigned images, save annotations

### Input

- **Image directory**: A local folder containing image files (JPG/PNG), optionally in nested subdirectories.

```
images/
├── folder1/
│   ├── page1.jpg
│   └── page2.png
├── folder2/
│   └── document.jpg
└── scan.png
```

### Output

- **YOLO export**: Admin can export approved images and label files as a zip.

### Annotation Classes (14 total)

0. Header
1. Footer
2. Title
3. Text
4. Table
5. Figure
6. Caption
7. Equation
8. List Item
9. Page Number
10. Section Header
11. Key-Value Pair
12. Signature
13. Seal

### YOLO Label Format

Each label file contains one line per bounding box:

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

## Running the Application

```bash
python app.py
```

Open your browser to: `http://localhost:8000`

On first run, the app creates the SQLite database and seeds default users:
- **Admin**: `VioletSkeleton` / `Welcome@123`
- **OA**: `UnsiloedOA1` / `Hello123`

New annotators can self-register via the login page.

## Usage Workflow

### Admin
1. Login as admin, set the **Image Root Directory** to scan images.
2. Create OA and annotator users (or let them self-register).
3. Images are auto-distributed to OAs when scanned.
4. Monitor progress, export approved annotations as YOLO zip.

### OA
1. Add annotators to your team.
2. Distribute images to annotators (specify count per annotator).
3. Review submitted annotations — approve or reject.
4. Rejected images go back to the annotator for fixing.

### Annotator
1. Login or register as a new annotator.
2. View assigned images in the **Grid View**.
3. Click an image to annotate — draw bounding boxes, select classes.
4. **Save & Next** to submit and move to the next image.

### Annotation Controls
- **Draw Mode OFF**: Click on existing boxes to remove them.
- **Draw Mode ON**: Drag to draw rectangles around layout elements.
- After drawing, select a class from the popup.
- **Clear Region** removes all annotations overlapping the drawn area.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| D | Toggle Draw Mode |
| S | Save & Next |
| A | Approve (OA review) |
| R | Reject (OA review) |

## File Structure

```
s3/
├── app.py                  # Flask app factory, blueprint registration
├── models.py               # SQLAlchemy models (User, Image, Assignment, Annotation)
├── auth.py                 # Flask-Login, login/logout/register, role_required decorator
├── image_scanner.py        # Recursive image folder scanner
├── routes_admin.py         # Admin blueprint
├── routes_annotator.py     # Annotator blueprint
├── routes_oa.py            # OA blueprint
├── routes_api.py           # Shared API (image serving, annotations, review)
├── requirements.txt        # Python dependencies
├── instance/               # SQLite database (auto-created, gitignored)
├── static/
│   ├── css/style.css       # Shared styles
│   ├── js/canvas.js        # AnnotationCanvas class
│   ├── js/grid.js          # ImageGrid component
│   └── thumbnails/         # Cached thumbnails (auto-created, gitignored)
└── templates/
    ├── base.html           # Shared layout with role-based nav
    ├── login.html
    ├── register.html
    ├── admin/
    │   ├── dashboard.html
    │   └── user_detail.html
    ├── annotator/
    │   ├── annotate.html
    │   └── grid.html
    └── oa/
        ├── dashboard.html
        ├── review.html
        └── grid.html
```

## Status Lifecycle

```
pending → annotated → approved
                   ↘ rejected → pending (loops back for fixing)
```

Rejected images keep their existing annotations so the annotator can fix rather than redo.
