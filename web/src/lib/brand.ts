// Accent colour: the customer's brand colour, nudged until it stays readable on the theme.

type RGB = [number, number, number];
const hex = (h: string): RGB => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB;
const toHex = (c: RGB) => "#" + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
const lum = (c: RGB) => {
  const ch = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2]);
};
export const contrast = (a: RGB, b: RGB) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

/** Lighten (dark theme) or darken (light theme) until accent text on the background is >= 4.5:1. */
export function readableAccent(accent: string, theme: "dark" | "light"): string {
  if (!/^#[0-9a-fA-F]{6}$/.test(accent)) return "";
  const bg: RGB = theme === "dark" ? [11, 14, 20] : [255, 255, 255];
  let c = hex(accent);
  for (let i = 0; i < 40 && contrast(c, bg) < 4.5; i++) {
    c = theme === "dark" ? c.map((v) => v + (255 - v) * 0.08) as RGB : c.map((v) => v * 0.92) as RGB;
  }
  return toHex(c);
}

export function applyAccent(accent: string, theme: "dark" | "light") {
  const root = document.documentElement.style;
  const value = readableAccent(accent, theme);
  if (!value) { root.removeProperty("--accent"); root.removeProperty("--accent-weak"); return; }
  root.setProperty("--accent", value);
  root.setProperty("--accent-weak", value + "22");
}

/** Fired after branding changes so the shell reloads the logo and colour. */
export const BRANDING_EVENT = "xplogent:branding";
