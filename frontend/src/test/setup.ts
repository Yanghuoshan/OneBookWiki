import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => cleanup());

Object.defineProperty(window.HTMLElement.prototype, 'setPointerCapture', {
  configurable: true,
  value: () => undefined,
});
Object.defineProperty(window.HTMLElement.prototype, 'releasePointerCapture', {
  configurable: true,
  value: () => undefined,
});
Object.defineProperty(window.HTMLElement.prototype, 'hasPointerCapture', {
  configurable: true,
  value: () => false,
});

// jsdom has no native PointerEvent; polyfill it from MouseEvent so
// fireEvent.pointerDown/Move/Up preserve pointerId/isPrimary/button.
if (typeof window.PointerEvent === 'undefined') {
  class PointerEventPolyfill extends MouseEvent {
    public pointerId?: number;
    public isPrimary?: boolean;
    public pointerType?: string;

    constructor(type: string, params: PointerEventInit = {}) {
      super(type, params);
      this.pointerId = params.pointerId;
      this.isPrimary = params.isPrimary;
      this.pointerType = params.pointerType;
    }
  }
  // @ts-expect-error -- assigning a polyfill onto the jsdom window
  window.PointerEvent = PointerEventPolyfill;
}
