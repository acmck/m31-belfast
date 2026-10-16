/**
 * <compare-slider>: a before/after image comparison.
 *
 * Markup (server-rendered, works without JS as a stacked pair):
 *   <compare-slider data-before="…" data-after="…">
 *     <div class="cs-frame">
 *       <div class="cs-after">…</div>
 *       <div class="cs-before">…</div>
 *       <div class="cs-handle" role="slider" tabindex="0" …>
 *         <span class="cs-read cs-read-before">…</span>
 *         <span class="cs-read cs-read-after">…</span>
 *       </div>
 *     </div>
 *   </compare-slider>
 *
 * The position lives in a CSS custom property (--pos, 0–100) on the host; the
 * before layer is clipped to the left of it with clip-path.
 */
class CompareSlider extends HTMLElement {
  #pos = 50;
  #frame: HTMLElement | null = null;
  #handle: HTMLElement | null = null;
  #dragging = false;
  #raf = 0;
  #pending = 50;

  connectedCallback(): void {
    this.#frame = this.querySelector('.cs-frame');
    this.#handle = this.querySelector('.cs-handle');
    if (!this.#frame || !this.#handle) return;

    const start = Number(this.getAttribute('value') ?? '50');
    this.#set(Number.isFinite(start) ? start : 50);
    this.classList.add('is-ready');

    this.#frame.addEventListener('pointerdown', this.#onDown);
    this.#frame.addEventListener('pointermove', this.#onMove);
    this.#frame.addEventListener('pointerup', this.#onUp);
    this.#frame.addEventListener('pointercancel', this.#onUp);
    this.#frame.addEventListener('lostpointercapture', this.#onUp);
    this.#handle.addEventListener('keydown', this.#onKey);
  }

  disconnectedCallback(): void {
    cancelAnimationFrame(this.#raf);
  }

  #fromEvent(e: PointerEvent): number {
    const r = this.#frame!.getBoundingClientRect();
    return ((e.clientX - r.left) / r.width) * 100;
  }

  #onDown = (e: PointerEvent): void => {
    if (e.button !== 0) return;
    this.#dragging = true;
    this.#frame!.setPointerCapture(e.pointerId);
    this.classList.add('is-dragging');
    this.#schedule(this.#fromEvent(e));
    // Keep keyboard users and screen readers in sync with where the pointer put it.
    this.#handle!.focus({ preventScroll: true });
    e.preventDefault();
  };

  #onMove = (e: PointerEvent): void => {
    if (!this.#dragging) return;
    this.#schedule(this.#fromEvent(e));
  };

  #onUp = (): void => {
    if (!this.#dragging) return;
    this.#dragging = false;
    this.classList.remove('is-dragging');
  };

  #onKey = (e: KeyboardEvent): void => {
    const step = e.shiftKey ? 10 : 2;
    let next: number | null = null;
    switch (e.key) {
      case 'ArrowLeft':
      case 'ArrowDown':
        next = this.#pos - step;
        break;
      case 'ArrowRight':
      case 'ArrowUp':
        next = this.#pos + step;
        break;
      case 'PageDown':
        next = this.#pos - 10;
        break;
      case 'PageUp':
        next = this.#pos + 10;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = 100;
        break;
      default:
        return;
    }
    e.preventDefault();
    this.#set(next);
  };

  #schedule(v: number): void {
    this.#pending = v;
    if (this.#raf) return;
    this.#raf = requestAnimationFrame(() => {
      this.#raf = 0;
      this.#set(this.#pending);
    });
  }

  #set(v: number): void {
    const pos = Math.round(Math.min(100, Math.max(0, v)) * 10) / 10;
    this.#pos = pos;
    this.style.setProperty('--pos', String(pos));
    // Which side is showing more, for the caption row on small screens.
    this.dataset.side = pos > 55 ? 'before' : pos < 45 ? 'after' : 'even';
    // Readouts flip inward near the edges so they stay on screen.
    this.dataset.edge = pos < 30 ? 'left' : pos > 70 ? 'right' : '';

    const h = this.#handle!;
    const rounded = Math.round(pos);
    h.setAttribute('aria-valuenow', String(rounded));
    const before = this.dataset.before ?? 'before';
    const after = this.dataset.after ?? 'after';
    let text: string;
    if (rounded <= 0) text = `Showing only ${after}`;
    else if (rounded >= 100) text = `Showing only ${before}`;
    else text = `${rounded} percent ${before} on the left, ${100 - rounded} percent ${after} on the right`;
    h.setAttribute('aria-valuetext', text);
  }
}

if (!customElements.get('compare-slider')) {
  customElements.define('compare-slider', CompareSlider);
}
