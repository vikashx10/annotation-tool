/**
 * AnnotationCanvas - Reusable canvas annotation component.
 *
 * Usage:
 *   const ac = new AnnotationCanvas('annotationCanvas', {
 *       classNames: [...],
 *       classColors: [...],
 *       readOnly: false,
 *       onSave: async (imageId, annotations) => { ... },
 *       getImageUrl: (imageId) => `/api/image/${imageId}`,
 *       getAnnotationsUrl: (imageId) => `/api/annotations/${imageId}`,
 *   });
 *   ac.loadImage(imageId);
 */
class AnnotationCanvas {
    constructor(canvasId, options = {}) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        this.classNames = options.classNames || [];
        this.classColors = options.classColors || [];
        this.readOnly = options.readOnly || false;
        this.onSave = options.onSave || null;
        this.getImageUrl = options.getImageUrl || ((id) => `/api/image/${id}`);
        this.getAnnotationsUrl = options.getAnnotationsUrl || ((id) => `/api/annotations/${id}`);
        this.onAnnotationsChanged = options.onAnnotationsChanged || null;

        this.image = new Image();
        this.currentImageId = null;
        this.annotations = [];
        this.drawMode = false;
        this.isDrawing = false;
        this.startX = 0;
        this.startY = 0;
        this.pendingBox = null;

        this.zoom = 1;
        this.minZoom = 0.3;
        this.maxZoom = 5;
        this._baseWidth = 0;
        this._baseHeight = 0;
        this._touchMode = 'none';   // 'none' | 'draw' | 'pinch'
        this._pinchStartDist = 0;
        this._pinchStartZoom = 1;
        this._lastTouchX = 0;
        this._lastTouchY = 0;
        this._lastPinchMid = null;

        this._setupEventListeners();
        this._createZoomBadge();
    }

    _setupEventListeners() {
        if (this.readOnly) {
            this._setupZoomListeners();
            return;
        }

        this.canvas.addEventListener('mousedown', (e) => this._handleMouseDown(e), false);
        this.canvas.addEventListener('mousemove', (e) => this._handleMouseMove(e), false);
        this.canvas.addEventListener('mouseup', (e) => this._handleMouseUp(e), false);

        this.canvas.addEventListener('touchstart', (e) => this._handleTouchStart(e), { passive: false });
        this.canvas.addEventListener('touchmove', (e) => this._handleTouchMove(e), { passive: false });
        this.canvas.addEventListener('touchend', (e) => this._handleTouchEnd(e), { passive: false });
        this.canvas.addEventListener('touchcancel', (e) => this._handleTouchEnd(e), { passive: false });

        this._setupZoomListeners();

        const overlay = document.getElementById('overlay');
        if (overlay) {
            overlay.addEventListener('click', () => this.cancelDrawing());
        }
    }

    _setupZoomListeners() {
        const scroll = this.canvas.closest('.canvas-scroll');
        if (scroll) {
            scroll.addEventListener('wheel', (e) => this._handleWheel(e), { passive: false });
        }

        this.canvas.addEventListener('dblclick', () => this.resetZoom());
    }

    /* ── Touch handling (single-finger draw, two-finger pinch zoom) ── */

    _handleTouchStart(e) {
        if (e.touches.length >= 2) {
            e.preventDefault();
            this._touchMode = 'pinch';
            this.isDrawing = false;
            this.pendingBox = null;
            this._pinchStartDist = this._getTouchDist(e.touches[0], e.touches[1]);
            this._pinchStartZoom = this.zoom;
            this._lastPinchMid = this._getTouchMid(e.touches[0], e.touches[1]);
            return;
        }
        if (e.touches.length === 1 && this._touchMode !== 'pinch') {
            e.preventDefault();
            this._touchMode = 'draw';
            const t = e.touches[0];
            this._lastTouchX = t.clientX;
            this._lastTouchY = t.clientY;
            this._handleMouseDown({ clientX: t.clientX, clientY: t.clientY, preventDefault() {}, stopPropagation() {} });
        }
    }

    _handleTouchMove(e) {
        if (this._touchMode === 'pinch' && e.touches.length >= 2) {
            e.preventDefault();
            const dist = this._getTouchDist(e.touches[0], e.touches[1]);
            const mid = this._getTouchMid(e.touches[0], e.touches[1]);

            const raw = dist / this._pinchStartDist;
            const dampened = 1 + (raw - 1) * 0.5;
            const newZoom = Math.max(this.minZoom, Math.min(this.maxZoom,
                this._pinchStartZoom * dampened));
            this._zoomAtPoint(newZoom, mid.x, mid.y);

            if (this._lastPinchMid) {
                const scroll = this.canvas.closest('.canvas-scroll');
                if (scroll) {
                    scroll.scrollLeft -= (mid.x - this._lastPinchMid.x);
                    scroll.scrollTop -= (mid.y - this._lastPinchMid.y);
                }
            }
            this._lastPinchMid = mid;
            return;
        }
        if (this._touchMode === 'draw' && e.touches.length === 1) {
            e.preventDefault();
            const t = e.touches[0];
            this._lastTouchX = t.clientX;
            this._lastTouchY = t.clientY;
            this._handleMouseMove({ clientX: t.clientX, clientY: t.clientY, preventDefault() {}, stopPropagation() {} });
        }
    }

    _handleTouchEnd(e) {
        if (e.touches.length === 0) {
            if (this._touchMode === 'draw') {
                this._handleMouseUp({ clientX: this._lastTouchX, clientY: this._lastTouchY, preventDefault() {}, stopPropagation() {} });
            }
            this._touchMode = 'none';
        }
    }

    _getTouchDist(a, b) {
        const dx = a.clientX - b.clientX;
        const dy = a.clientY - b.clientY;
        return Math.sqrt(dx * dx + dy * dy);
    }

    _getTouchMid(a, b) {
        return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
    }

    /* ── Mouse wheel zoom ── */

    _handleWheel(e) {
        if (!e.ctrlKey && !e.metaKey) return;
        e.preventDefault();
        const factor = e.deltaY > 0 ? 0.97 : 1.03;
        const newZoom = Math.max(this.minZoom, Math.min(this.maxZoom, this.zoom * factor));
        this._zoomAtPoint(newZoom, e.clientX, e.clientY);
    }

    /* ── Zoom helpers ── */

    _zoomAtPoint(newZoom, screenX, screenY) {
        const scroll = this.canvas.closest('.canvas-scroll');
        if (!scroll || !this._baseWidth) return;

        const scrollRect = scroll.getBoundingClientRect();
        const viewX = screenX - scrollRect.left;
        const viewY = screenY - scrollRect.top;
        const contentX = viewX + scroll.scrollLeft;
        const contentY = viewY + scroll.scrollTop;
        const fracX = contentX / (this._baseWidth * this.zoom);
        const fracY = contentY / (this._baseHeight * this.zoom);

        this.zoom = newZoom;
        this._applyZoom();

        scroll.scrollLeft = fracX * this._baseWidth * this.zoom - viewX;
        scroll.scrollTop = fracY * this._baseHeight * this.zoom - viewY;
    }

    _applyZoom() {
        if (!this._baseWidth || !this._baseHeight) return;
        this.canvas.style.width = Math.round(this._baseWidth * this.zoom) + 'px';
        this.canvas.style.height = Math.round(this._baseHeight * this.zoom) + 'px';
        this._updateZoomIndicator();
    }

    resetZoom() {
        this.zoom = 1;
        this._applyZoom();
        const scroll = this.canvas.closest('.canvas-scroll');
        if (scroll) { scroll.scrollLeft = 0; scroll.scrollTop = 0; }
    }

    _createZoomBadge() {
        const area = this.canvas.closest('.annotate-canvas-area');
        if (!area) return;

        let toolbar = area.querySelector('.annotate-toolbar');
        if (!toolbar) {
            toolbar = document.createElement('div');
            toolbar.className = 'annotate-toolbar';
            const scroll = area.querySelector('.canvas-scroll');
            area.insertBefore(toolbar, scroll);
        }

        const badge = document.createElement('div');
        badge.className = 'zoom-badge';
        badge.innerHTML = `
            <button id="zoomOutBtn" title="Zoom out">&#x2212;</button>
            <span id="zoomBadge">100%</span>
            <button id="zoomInBtn" title="Zoom in">&#x2b;</button>
            <button id="zoomResetBtn" title="Reset zoom (double-click)">&#x21ba;</button>`;
        toolbar.appendChild(badge);

        badge.querySelector('#zoomOutBtn').addEventListener('click', () => {
            const newZoom = Math.max(this.minZoom, this.zoom * 0.8);
            this.zoom = newZoom;
            this._applyZoom();
        });
        badge.querySelector('#zoomInBtn').addEventListener('click', () => {
            const newZoom = Math.min(this.maxZoom, this.zoom * 1.25);
            this.zoom = newZoom;
            this._applyZoom();
        });
        badge.querySelector('#zoomResetBtn').addEventListener('click', () => this.resetZoom());
    }

    _updateZoomIndicator() {
        const badge = document.getElementById('zoomBadge');
        if (!badge) return;
        const pct = Math.round(this.zoom * 100);
        badge.textContent = pct + '%';
    }

    async loadImage(imageId) {
        this.currentImageId = imageId;
        this.pendingBox = null;
        this.zoom = 1;

        // Fetch annotations
        try {
            const resp = await fetch(this.getAnnotationsUrl(imageId));
            this.annotations = await resp.json();
        } catch (e) {
            this.annotations = [];
        }

        // Load image
        return new Promise((resolve, reject) => {
            this.image = new Image();
            this.image.onload = () => {
                this.canvas.width = this.image.width;
                this.canvas.height = this.image.height;
                this._fitToContainer();
                this.draw();
                resolve();
            };
            this.image.onerror = () => {
                reject(new Error('Failed to load image'));
            };
            this.image.src = this.getImageUrl(imageId);
        });
    }

    draw() {
        const { ctx, canvas, image, annotations, pendingBox, classColors } = this;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);

        annotations.forEach((ann) => {
            const x = (ann.x_center - ann.width / 2) * canvas.width;
            const y = (ann.y_center - ann.height / 2) * canvas.height;
            const w = ann.width * canvas.width;
            const h = ann.height * canvas.height;

            const color = classColors[ann.class_id] || '#000000';
            ctx.strokeStyle = color;
            ctx.lineWidth = 3;
            ctx.strokeRect(x, y, w, h);

            // Draw class label
            const label = this.classNames[ann.class_id] || `Class ${ann.class_id}`;
            const fontSize = Math.max(12, Math.min(16, canvas.width / 60));
            ctx.font = `bold ${fontSize}px sans-serif`;
            const textWidth = ctx.measureText(label).width;
            const pad = 4;

            // Background pill behind text
            ctx.fillStyle = color;
            ctx.fillRect(x, y - fontSize - pad * 2, textWidth + pad * 2, fontSize + pad * 2);

            // White text
            ctx.fillStyle = '#ffffff';
            ctx.fillText(label, x + pad, y - pad);
        });

        if (pendingBox) {
            const x = (pendingBox.x_center - pendingBox.width / 2) * canvas.width;
            const y = (pendingBox.y_center - pendingBox.height / 2) * canvas.height;
            const w = pendingBox.width * canvas.width;
            const h = pendingBox.height * canvas.height;

            ctx.strokeStyle = '#ff0000';
            ctx.lineWidth = 3;
            ctx.setLineDash([5, 5]);
            ctx.strokeRect(x, y, w, h);
            ctx.setLineDash([]);
        }
    }

    _fitToContainer() {
        const scroll = this.canvas.closest('.canvas-scroll');
        if (!scroll) return;
        const availW = scroll.clientWidth - 48;
        const availH = scroll.clientHeight - 48;
        const imgW = this.canvas.width;
        const imgH = this.canvas.height;
        if (!availW || !availH || !imgW || !imgH) return;
        const scale = Math.min(availW / imgW, availH / imgH);
        this._baseWidth = Math.round(imgW * scale);
        this._baseHeight = Math.round(imgH * scale);
        this.canvas.style.width  = Math.round(this._baseWidth * this.zoom) + 'px';
        this.canvas.style.height = Math.round(this._baseHeight * this.zoom) + 'px';
        this._updateZoomIndicator();
    }

    toggleDrawMode() {
        this.drawMode = !this.drawMode;
        this.canvas.style.cursor = this.drawMode ? 'crosshair' : 'pointer';
        return this.drawMode;
    }

    setDrawMode(on) {
        this.drawMode = on;
        this.canvas.style.cursor = on ? 'crosshair' : 'pointer';
    }

    _getCanvasCoords(e) {
        const rect = this.canvas.getBoundingClientRect();
        const scaleX = this.canvas.width / rect.width;
        const scaleY = this.canvas.height / rect.height;
        return {
            x: (e.clientX - rect.left) * scaleX,
            y: (e.clientY - rect.top) * scaleY,
        };
    }

    _handleMouseDown(e) {
        e.preventDefault();
        e.stopPropagation();

        if (!this.canvas || this.canvas.width === 0 || this.canvas.height === 0) return;

        const { x: canvasX, y: canvasY } = this._getCanvasCoords(e);
        if (canvasX < 0 || canvasX > this.canvas.width || canvasY < 0 || canvasY > this.canvas.height) return;

        if (!this.drawMode) {
            // Click to select box — show options (change label or remove)
            const normalizedX = canvasX / this.canvas.width;
            const normalizedY = canvasY / this.canvas.height;

            for (let i = this.annotations.length - 1; i >= 0; i--) {
                const ann = this.annotations[i];
                const boxX = ann.x_center - ann.width / 2;
                const boxY = ann.y_center - ann.height / 2;
                const boxX2 = ann.x_center + ann.width / 2;
                const boxY2 = ann.y_center + ann.height / 2;

                if (normalizedX >= boxX && normalizedX <= boxX2 &&
                    normalizedY >= boxY && normalizedY <= boxY2) {
                    this._selectedAnnotationIndex = i;
                    this._showRelabelSelector(i);
                    break;
                }
            }
        } else {
            this.pendingBox = null;
            this.isDrawing = true;
            this.startX = canvasX / this.canvas.width;
            this.startY = canvasY / this.canvas.height;
        }
    }

    _handleMouseMove(e) {
        if (this.drawMode && this.isDrawing) {
            e.preventDefault();
            e.stopPropagation();
            const { x, y } = this._getCanvasCoords(e);
            const currentX = x / this.canvas.width;
            const currentY = y / this.canvas.height;

            const x1 = Math.min(this.startX, currentX);
            const y1 = Math.min(this.startY, currentY);
            const x2 = Math.max(this.startX, currentX);
            const y2 = Math.max(this.startY, currentY);

            this.pendingBox = {
                x_center: (x1 + x2) / 2,
                y_center: (y1 + y2) / 2,
                width: x2 - x1,
                height: y2 - y1
            };

            this.draw();
        }
    }

    _handleMouseUp(e) {
        if (this.drawMode && this.isDrawing) {
            e.preventDefault();
            this.isDrawing = false;

            const { x, y } = this._getCanvasCoords(e);
            const endX = x / this.canvas.width;
            const endY = y / this.canvas.height;

            const x1 = Math.min(this.startX, endX);
            const y1 = Math.min(this.startY, endY);
            const x2 = Math.max(this.startX, endX);
            const y2 = Math.max(this.startY, endY);

            const width = x2 - x1;
            const height = y2 - y1;

            if (width > 0.01 && height > 0.01) {
                this.pendingBox = {
                    x_center: x1 + width / 2,
                    y_center: y1 + height / 2,
                    width: width,
                    height: height
                };
                this._showClassSelector();
                this.draw();
            }
        }
    }

    _showClassSelector() {
        const buttonsDiv = document.getElementById('classButtons');
        if (!buttonsDiv) return;
        buttonsDiv.innerHTML = '';

        this.classNames.forEach((name, idx) => {
            const btn = document.createElement('button');
            btn.className = 'class-btn';
            btn.textContent = `${idx}: ${name}`;
            btn.style.borderColor = this.classColors[idx];
            btn.style.color = this.classColors[idx];
            btn.onclick = () => this.selectClass(idx);
            buttonsDiv.appendChild(btn);
        });

        document.getElementById('overlay').classList.add('show');
        document.getElementById('classSelector').classList.add('show');
    }

    _showRelabelSelector(annotationIndex) {
        const buttonsDiv = document.getElementById('classButtons');
        if (!buttonsDiv) return;
        buttonsDiv.innerHTML = '';

        const currentClassId = this.annotations[annotationIndex].class_id;
        const currentName = this.classNames[currentClassId] || `Class ${currentClassId}`;

        // Header showing current label
        const header = document.createElement('div');
        header.style.cssText = 'width:100%; text-align:center; font-size:0.85rem; color:#666; margin-bottom:8px; padding-bottom:8px; border-bottom:1px solid #e5e7eb;';
        header.innerHTML = `Current: <strong style="color:${this.classColors[currentClassId]}">${currentName}</strong>`;
        buttonsDiv.appendChild(header);

        // Class buttons for relabeling
        this.classNames.forEach((name, idx) => {
            const btn = document.createElement('button');
            btn.className = 'class-btn';
            btn.textContent = `${idx}: ${name}`;
            btn.style.borderColor = this.classColors[idx];
            btn.style.color = this.classColors[idx];
            if (idx === currentClassId) {
                btn.style.background = this.classColors[idx];
                btn.style.color = '#fff';
            }
            btn.onclick = () => this._relabelAnnotation(annotationIndex, idx);
            buttonsDiv.appendChild(btn);
        });

        // Delete button
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'class-btn';
        deleteBtn.textContent = 'Delete This Box';
        deleteBtn.style.cssText = 'border-color:#dc3545; color:#dc3545; font-weight:700; margin-top:8px; width:100%;';
        deleteBtn.onclick = () => {
            this.annotations.splice(annotationIndex, 1);
            document.getElementById('overlay').classList.remove('show');
            document.getElementById('classSelector').classList.remove('show');
            this._selectedAnnotationIndex = null;
            this.draw();
            this._notifyChanged();
        };
        buttonsDiv.appendChild(deleteBtn);

        document.getElementById('overlay').classList.add('show');
        document.getElementById('classSelector').classList.add('show');
    }

    _relabelAnnotation(index, newClassId) {
        if (index >= 0 && index < this.annotations.length) {
            this.annotations[index].class_id = newClassId;
        }
        this._selectedAnnotationIndex = null;
        document.getElementById('overlay').classList.remove('show');
        document.getElementById('classSelector').classList.remove('show');
        this.draw();
        this._notifyChanged();
    }

    selectClass(classId) {
        if (this.pendingBox) {
            this.annotations.push({
                class_id: classId,
                x_center: this.pendingBox.x_center,
                y_center: this.pendingBox.y_center,
                width: this.pendingBox.width,
                height: this.pendingBox.height
            });
            this.pendingBox = null;
            document.getElementById('overlay').classList.remove('show');
            document.getElementById('classSelector').classList.remove('show');
            this.draw();
            this._notifyChanged();
        }
    }

    cancelDrawing() {
        this.pendingBox = null;
        this._selectedAnnotationIndex = null;
        document.getElementById('overlay').classList.remove('show');
        document.getElementById('classSelector').classList.remove('show');
        this.draw();
    }

    clearRegion() {
        if (this.pendingBox) {
            const regionLeft = this.pendingBox.x_center - this.pendingBox.width / 2;
            const regionRight = this.pendingBox.x_center + this.pendingBox.width / 2;
            const regionTop = this.pendingBox.y_center - this.pendingBox.height / 2;
            const regionBottom = this.pendingBox.y_center + this.pendingBox.height / 2;

            let removedCount = 0;
            this.annotations = this.annotations.filter(ann => {
                const boxLeft = ann.x_center - ann.width / 2;
                const boxRight = ann.x_center + ann.width / 2;
                const boxTop = ann.y_center - ann.height / 2;
                const boxBottom = ann.y_center + ann.height / 2;

                const overlaps = !(boxRight < regionLeft ||
                                  boxLeft > regionRight ||
                                  boxBottom < regionTop ||
                                  boxTop > regionBottom);

                if (overlaps) {
                    removedCount++;
                    return false;
                }
                return true;
            });

            this.pendingBox = null;
            document.getElementById('overlay').classList.remove('show');
            document.getElementById('classSelector').classList.remove('show');
            this.draw();
            this._notifyChanged();
            return removedCount;
        }
        return 0;
    }

    async save() {
        if (!this.currentImageId || !this.onSave) return false;
        try {
            await this.onSave(this.currentImageId, this.annotations);
            return true;
        } catch (e) {
            console.error('Save failed:', e);
            return false;
        }
    }

    getAnnotations() {
        return this.annotations;
    }

    getAnnotationCount() {
        return this.annotations.length;
    }

    loadFromCache(imageId, imgElement, annotations) {
        /**
         * Instant load from prefetched data — no network calls.
         * imgElement must be a fully loaded HTMLImageElement.
         */
        this.currentImageId = imageId;
        this.pendingBox = null;
        this.zoom = 1;
        this.annotations = JSON.parse(JSON.stringify(annotations)); // deep copy
        this.image = imgElement;
        this.canvas.width = imgElement.naturalWidth;
        this.canvas.height = imgElement.naturalHeight;
        this._fitToContainer();
        this.draw();
        this._notifyChanged();
    }

    removeAnnotation(index) {
        if (index >= 0 && index < this.annotations.length) {
            this.annotations.splice(index, 1);
            this.draw();
            this._notifyChanged();
        }
    }

    _notifyChanged() {
        if (this.onAnnotationsChanged) {
            this.onAnnotationsChanged(this.annotations);
        }
    }
}
