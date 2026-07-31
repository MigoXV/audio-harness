import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

class TestResizeObserver {
  constructor(callback: ResizeObserverCallback) {
    void callback;
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): ResizeObserverEntry[] {
    return [];
  }
}

globalThis.ResizeObserver = TestResizeObserver;

afterEach(cleanup);
