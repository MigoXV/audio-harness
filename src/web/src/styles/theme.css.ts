import { createGlobalTheme, createThemeContract } from "@vanilla-extract/css";

export const vars = createThemeContract({
  color: {
    canvas: null, surface: null, surfaceMuted: null, text: null, textMuted: null, textFaint: null,
    line: null, lineStrong: null, accent: null, accentHover: null, accentSoft: null,
    success: null, successSoft: null, warning: null, warningSoft: null, danger: null, dangerSoft: null,
    overlay: null, inverse: null,
  },
  space: { x1: null, x2: null, x3: null, x4: null, x5: null, x6: null, x8: null, x12: null },
  radius: { sm: null, md: null, pill: null },
  font: { body: null, mono: null },
  shadow: { floating: null },
  motion: { fast: null },
});

createGlobalTheme(":root", vars, {
  color: {
    canvas: "#f7f7f5", surface: "#fbfbf9", surfaceMuted: "#f1f2ef", text: "#202522",
    textMuted: "#69716c", textFaint: "#929993", line: "rgba(17, 24, 39, 0.09)",
    lineStrong: "rgba(17, 24, 39, 0.14)", accent: "#28684f", accentHover: "#20543f",
    accentSoft: "#eaf2ed", success: "#287154", successSoft: "#eaf2ed", warning: "#9a6a20",
    warningSoft: "#f7f0e2", danger: "#a1453f", dangerSoft: "#f7eae8", overlay: "rgba(25, 30, 27, .34)", inverse: "#fff",
  },
  space: { x1: "4px", x2: "8px", x3: "12px", x4: "16px", x5: "20px", x6: "24px", x8: "32px", x12: "48px" },
  radius: { sm: "4px", md: "7px", pill: "999px" },
  font: { body: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif', mono: '"SFMono-Regular", Consolas, "Liberation Mono", monospace' },
  shadow: { floating: "0 12px 32px rgba(25, 30, 27, .14)" },
  motion: { fast: "160ms" },
});
