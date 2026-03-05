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

        this._setupEventListeners();
    }

    _setupEventListeners() {
        if (this.readOnly) return;

        this.canvas.addEventListener('mousedown', (e) => this._handleMouseDown(e), false);
        this.canvas.addEventListener('mousemove', (e) => this._handleMouseMove(e), false);
        this.canvas.addEventListener('mouseup', (e) => this._handleMouseUp(e), false);

        this.canvas.addEventListener('touchstart', (e) => {
            e.preventDefault();
            const touch = e.touches[0];
            this.canvas.dispatchEvent(new MouseEvent('mousedown', {
                clientX: touch.clientX, clientY: touch.clientY
            }));
        }, false);

        this.canvas.addEventListener('touchmove', (e) => {
            e.preventDefault();
            const touch = e.touches[0];
            this.canvas.dispatchEvent(new MouseEvent('mousemove', {
                clientX: touch.clientX, clientY: touch.clientY
            }));
        }, false);

        this.canvas.addEventListener('touchend', (e) => {
            e.preventDefault();
            this.canvas.dispatchEvent(new MouseEvent('mouseup', {}));
        }, false);

        const overlay = document.getElementById('overlay');
        if (overlay) {
            overlay.addEventListener('click', () => this.cancelDrawing());
        }
    }

    async loadImage(imageId) {
        this.currentImageId = imageId;
        this.pendingBox = null;

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
        const availW = scroll.clientWidth - 48;   // 24px padding each side
        const availH = scroll.clientHeight - 48;
        const imgW = this.canvas.width;
        const imgH = this.canvas.height;
        if (!availW || !availH || !imgW || !imgH) return;
        const scale = Math.min(availW / imgW, availH / imgH);
        this.canvas.style.width  = Math.round(imgW * scale) + 'px';
        this.canvas.style.height = Math.round(imgH * scale) + 'px';
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
            // Click to remove box
            const normalizedX = canvasX / this.canvas.width;
            const normalizedY = canvasY / this.canvas.height;

            let boxRemoved = false;
            for (let i = this.annotations.length - 1; i >= 0; i--) {
                const ann = this.annotations[i];
                const boxX = ann.x_center - ann.width / 2;
                const boxY = ann.y_center - ann.height / 2;
                const boxX2 = ann.x_center + ann.width / 2;
                const boxY2 = ann.y_center + ann.height / 2;

                if (normalizedX >= boxX && normalizedX <= boxX2 &&
                    normalizedY >= boxY && normalizedY <= boxY2) {
                    this.annotations.splice(i, 1);
                    boxRemoved = true;
                    break;
                }
            }

            if (boxRemoved) {
                this.draw();
                this._notifyChanged();
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
