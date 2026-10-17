import { createGlobalTheme, createThemeContract } from "@vanilla-extract/css";
import tokens from "./av.tokens.json";

export const vars = createThemeContract({
  color: {
    canvas: null, quietSurface: null, surface: null, surfaceMuted: null, text: null, textMuted: null, textFaint: null,
    line: null, lineStrong: null, accent: null, accentHover: null, accentSoft: null,
    success: null, successSoft: null, warning: null, warningSoft: null, danger: null, dangerSoft: null,
    overlay: null, inverse: null,
    selected: null, hover: null, focus: null, primaryBg: null, primaryText: null,
    roleInput: null, roleProcessor: null, roleOutput: null,
    portAudio: null, portSegment: null, portTranscript: null, portEvent: null, portVector: null,
  },
  weight: { heading: null, item: null },
  space: { x1: null, x2: null, x3: null, x4: null, x5: null, x6: null, x8: null, x12: null },
  radius: { sm: null, md: null, pill: null },
  font: { body: null, mono: null },
  shadow: { floating: null },
  motion: { fast: null },
});

const common = {
  space: { x1: "4px", x2: "8px", x3: "12px", x4: "16px", x5: "20px", x6: "24px", x8: "32px", x12: "48px" },
  radius: { sm: "6px", md: "8px", pill: "999px" },
  font: { body: '"Inter Variable", Inter, "Noto Sans SC", system-ui, sans-serif', mono: 'Cousine, "SFMono-Regular", Consolas, monospace' },
  motion: { fast: "160ms" },
};

for (const theme of ["vallum", "abyssus"] as const) {
  const palette = tokens.semantic[theme];
  const color = (role: keyof typeof palette) => palette[role].$value;
  createGlobalTheme(theme === "vallum" ? ':root, [data-theme="vallum"]' : '[data-theme="abyssus"]', vars, {
    ...common,
    weight: { heading: theme === "vallum" ? "600" : "700", item: theme === "vallum" ? "500" : "700" },
    color: {
      canvas: color("bgPrimary"), quietSurface: theme === "vallum" ? color("bgPrimary") : color("surfaceSubtle"), surface: color("surface"), surfaceMuted: color("surfaceSubtle"),
      text: color("textPrimary"), textMuted: color("textSecondary"), textFaint: color("textSecondary"),
      line: color("border"), lineStrong: color("borderStrong"), accent: color("accentStrong"),
      accentHover: color("accentStrong"), accentSoft: color("selected"), selected: color("selected"),
      hover: color("hover"), focus: color("focus"),
      primaryBg: theme === "vallum" ? tokens.color.vallum.inkBone.$value : color("accent"),
      primaryText: theme === "vallum" ? tokens.color.vallum.paleStone.$value : color("onAccent"),
      inverse: theme === "vallum" ? tokens.color.vallum.paleStone.$value : color("onAccent"),
      success: tokens.color.status.success.$value, successSoft: color("surfaceSubtle"),
      warning: tokens.color.status.warning.$value, warningSoft: color("surfaceSubtle"),
      danger: tokens.color.status.danger.$value, dangerSoft: color("surfaceSubtle"),
      overlay: `color-mix(in srgb, ${color("textPrimary")} 32%, transparent)`,
      roleInput: color("accentStrong"), roleProcessor: color("borderStrong"), roleOutput: color("textSecondary"),
      portAudio: color("accentStrong"), portSegment: color("textSecondary"),
      portTranscript: color("textSecondary"), portEvent: color("textSecondary"), portVector: color("accentStrong"),
    },
    shadow: { floating: `0 8px 24px color-mix(in srgb, ${color("textPrimary")} 12%, transparent)` },
  });
}
