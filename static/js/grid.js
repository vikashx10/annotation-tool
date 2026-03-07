/**
 * ImageGrid - Renders a paginated thumbnail grid with status filter.
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
        this.page = 1;
        this.pages = 1;
        this.statusFilter = '';
        this.loading = false;
    }

    async load(page = 1) {
        if (this.loading) return;
        this.loading = true;

        if (page === 1) {
            this.container.innerHTML = '<p style="padding:20px;color:#6c757d;">Loading...</p>';
        }

        try {
            const url = new URL(this.fetchUrl, location.origin);
            url.searchParams.set('page', page);
            if (this.statusFilter) url.searchParams.set('status', this.statusFilter);

            const resp = await fetch(url.toString());
            const data = await resp.json();

            this.page = data.page;
            this.pages = data.pages;

            if (page === 1) {
                this.render(data.images || [], data);
            } else {
                this.append(data.images || [], data);
            }
        } catch (e) {
            this.container.innerHTML = '<p style="padding:20px;color:#721c24;">Failed to load images.</p>';
        } finally {
            this.loading = false;
        }
    }

    render(images, meta) {
        this.container.innerHTML = '';

        if (images.length === 0) {
            this.container.innerHTML = '<p style="padding:20px;color:#6c757d;">No images found.</p>';
            return;
        }

        images.forEach(img => this.container.appendChild(this._makeCard(img)));
        this._updatePager(meta);
    }

    append(images, meta) {
        // Remove existing pager before appending
        const oldPager = this.container.querySelector('.grid-pager');
        if (oldPager) oldPager.remove();

        images.forEach(img => this.container.appendChild(this._makeCard(img)));
        this._updatePager(meta);
    }

    _makeCard(img) {
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
            // Prefetch full image on hover so it's in browser cache before navigation
            card.addEventListener('mouseenter', () => {
                fetch(`/api/image/${img.id}`, { credentials: 'same-origin' });
            }, { once: true });
            card.onclick = () => this.onClick(img.id);
        }
        return card;
    }

    _updatePager(meta) {
        if (!meta || meta.pages <= 1) return;

        const pager = document.createElement('div');
        pager.className = 'grid-pager';
        pager.style.cssText = 'width:100%; padding:16px; text-align:center; grid-column:1/-1;';

        if (meta.has_next) {
            const btn = document.createElement('button');
            btn.className = 'btn-secondary';
            btn.textContent = `Load more (page ${meta.page + 1} / ${meta.pages})`;
            btn.onclick = () => this.load(meta.page + 1);
            pager.appendChild(btn);
        } else {
            pager.innerHTML = `<span style="color:#6c757d;">All ${meta.total} images loaded</span>`;
        }

        this.container.appendChild(pager);
    }

    setStatusFilter(status) {
        this.statusFilter = status;
        this.load(1);
    }
}
