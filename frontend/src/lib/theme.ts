/** Theme preference: the only thing kept in browser storage, a display choice (10). */
export type Theme = "light" | "dark" | "system";
const THEME_KEY = "codehover-theme";

export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

export function applyStoredTheme(): void {
  const theme = readTheme();
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
}

export function storeTheme(theme: Theme): void {
  try {
    if (theme === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
  applyStoredTheme();
}
