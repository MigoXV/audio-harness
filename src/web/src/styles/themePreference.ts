export type Theme = "vallum" | "abyssus";

export const THEME_STORAGE_KEY = "audio-harness.theme";

export function readTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) === "abyssus" ? "abyssus" : "vallum";
  } catch {
    return "vallum";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme === "abyssus" ? "dark" : "light";
}

export function persistTheme(theme: Theme): void {
  applyTheme(theme);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 浏览器禁用存储时，当前会话仍可切换主题。
  }
}
