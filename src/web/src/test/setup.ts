import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

class TestResizeObserver {
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    this.callback([{ target, contentRect: target.getBoundingClientRect() } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
  unobserve() {}
  disconnect() {}
  takeRecords(): ResizeObserverEntry[] {
    return [];
  }
}

globalThis.ResizeObserver = TestResizeObserver;

class TestDOMMatrixReadOnly {
  readonly m22 = 1;
}

Object.defineProperty(window, "DOMMatrixReadOnly", { configurable: true, value: TestDOMMatrixReadOnly });
Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
  configurable: true,
  get() { return this.classList.contains("react-flow__handle") ? 12 : this.classList.contains("react-flow__node") ? 244 : 0; },
});
Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
  configurable: true,
  get() {
    if (this.classList.contains("react-flow__handle")) return 12;
    if (this.classList.contains("react-flow__node")) return 100 + this.querySelectorAll(".audio-port-row").length * 31;
    return 0;
  },
});

const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
  const width = this.offsetWidth;
  const height = this.offsetHeight;
  if (this.classList.contains("react-flow__handle")) {
    const node = this.closest(".react-flow__node");
    const sameType = node ? [...node.querySelectorAll(`.${this.classList.contains("source") ? "source" : "target"}`)] : [];
    const index = Math.max(0, sameType.indexOf(this));
    const left = this.classList.contains("source") ? 250 : -18;
    const top = 72 + index * 31;
    return { x: left, y: top, left, top, right: left + width, bottom: top + height, width, height, toJSON: () => ({}) };
  }
  if (this.classList.contains("react-flow__node")) {
    return { x: 0, y: 0, left: 0, top: 0, right: width, bottom: height, width, height, toJSON: () => ({}) };
  }
  return originalGetBoundingClientRect.call(this);
};

afterEach(cleanup);
