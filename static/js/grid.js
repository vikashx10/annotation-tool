/**
 * ImageGrid - Renders a thumbnail grid of images with status badges.
 *
 * Usage:
 *   const grid = new ImageGrid('gridContainer', {
 *       fetchUrl: '/annotator/grid_data',
 *       onClick: (imageId) => { window.location = `/annotator/annotate/${imageId}`; },
 *   });
 *   grid.load();
 */
class ImageGrid {
    constructor(containerId, options = {}) {
        this.container = document.getElementById(containerId);
        this.fetchUrl = options.fetchUrl || '/api/grid_data';
        this.onClick = options.onClick || null;
    }

    async load() {
        try {
            const resp = await fetch(this.fetchUrl);
            const data = await resp.json();
            this.render(data.images || []);
        } catch (e) {
            this.container.innerHTML = '<p style="padding:20px;color:#721c24;">Failed to load images.</p>';
        }
    }

    render(images) {
        this.container.innerHTML = '';

        if (images.length === 0) {
            this.container.innerHTML = '<p style="padding:20px;color:#6c757d;">No images assigned.</p>';
            return;
        }

        images.forEach(img => {
            const card = document.createElement('div');
            card.className = `grid-card status-${img.status}`;
            card.innerHTML = `
                <img src="/api/thumbnail/${img.id}" alt="${img.filename}" loading="lazy">
                <div class="card-info">
                    <div>${img.filename}</div>
                    <span class="badge badge-${img.status}">${img.status}</span>
                </div>
            `;
            if (this.onClick) {
                card.onclick = () => this.onClick(img.id);
            }
            this.container.appendChild(card);
        });
    }
}
