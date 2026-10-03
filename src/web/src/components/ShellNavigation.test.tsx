import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ShellNavigation } from "./ShellNavigation";

describe("workspace navigation", () => {
  it("offers a direct engine link on desktop and mobile", () => {
    render(<MemoryRouter initialEntries={["/engines"]}><ShellNavigation overview={null} failed={false} onRefresh={vi.fn()} /></MemoryRouter>);
    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(within(nav).getByRole("link", { name: "推理引擎" })).toHaveAttribute("aria-current", "page");
    expect(document.querySelector(".mobile-engine-link")).toHaveAttribute("href", "/engines");
    expect([...nav.querySelectorAll(".navigation-group-link")].map((link) => link.textContent)).toEqual(["语音工作流", "语音智能体", "推理引擎", "运行"]);
    expect(within(nav).getByRole("link", { name: "运行" })).toHaveAttribute("href", "/runtime");
  });

  it.each([["/vectors", "向量检索"], ["/agent-backends", "后端智能体"]])("marks resource %s and closes its menu with Escape", (path, name) => {
    render(<MemoryRouter initialEntries={[path]}><ShellNavigation overview={null} failed={false} onRefresh={vi.fn()} /></MemoryRouter>);
    const menu = document.querySelector<HTMLDetailsElement>(".navigation-resources")!;
    expect(menu).toHaveClass("active");
    expect(menu.querySelector("summary")).toHaveTextContent(`资源 / ${name}`);
    expect([...menu.querySelectorAll("a")].map((link) => link.textContent)).toEqual(["后端智能体", "向量检索"]);
    menu.open = true;
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(menu.open).toBe(false);
    expect(menu.querySelector("summary")).toHaveFocus();
    expect(within(menu).getByRole("link", { name, hidden: true })).toHaveAttribute("aria-current", "page");
  });

  it("closes resources after a selection or an outside click", () => {
    render(<MemoryRouter initialEntries={["/audio-workflows"]}><ShellNavigation overview={null} failed={false} onRefresh={vi.fn()} /></MemoryRouter>);
    const menu = document.querySelector<HTMLDetailsElement>(".navigation-resources")!;
    menu.open = true;
    fireEvent.click(within(menu).getByRole("link", { name: "后端智能体" }));
    expect(menu.open).toBe(false);
    expect(menu.querySelector("summary")).toHaveTextContent("资源 / 后端智能体");
    menu.open = true;
    fireEvent.pointerDown(screen.getByRole("button", { name: /切换到/ }));
    expect(menu.open).toBe(false);
  });
});
