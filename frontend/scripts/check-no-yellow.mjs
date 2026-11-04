import { readdirSync, readFileSync } from "node:fs";
import { extname, join, relative } from "node:path";

// The owner wants soft red for losses and warnings and no yellow/orange UI.
// Legacy amber/yellow Tailwind classes are remapped to --color-warning in
// index.css; this gate catches literal colors that would bypass those tokens.
const root = new URL("../src/", import.meta.url).pathname;
const failures = [];

function warmHue(r, g, b) {
  const max = Math.max(r, g, b) / 255;
  const min = Math.min(r, g, b) / 255;
  const delta = max - min;
  if (delta === 0 || delta / max < 0.35 || max < 0.3) return false;
  let hue;
  if (r / 255 === max) hue = ((g - b) / 255) / delta;
  else if (g / 255 === max) hue = ((b - r) / 255) / delta + 2;
  else hue = ((r - g) / 255) / delta + 4;
  hue = (hue * 60 + 360) % 360;
  return hue >= 20 && hue <= 80;
}

function scanFile(path) {
  const lines = readFileSync(path, "utf8").split("\n");
  lines.forEach((line, index) => {
    const colors = [];
    for (const [, raw] of line.matchAll(/#([\da-fA-F]{3,8})(?![\da-fA-F])/g)) {
      if (![3, 4, 6, 8].includes(raw.length)) continue;
      const rgb = raw.length <= 4
        ? [...raw.slice(0, 3)].map((digit) => parseInt(digit + digit, 16))
        : [0, 2, 4].map((offset) => parseInt(raw.slice(offset, offset + 2), 16));
      if (warmHue(...rgb)) colors.push(`#${raw}`);
    }
    for (const match of line.matchAll(/\brgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})/gi)) {
      if (warmHue(...match.slice(1, 4).map(Number))) colors.push(match[0]);
    }
    for (const match of line.matchAll(/\bhsla?\(\s*(\d+(?:\.\d+)?)\s*[, ]\s*(\d+(?:\.\d+)?)%/gi)) {
      const hue = Number(match[1]) % 360;
      if (hue >= 20 && hue <= 80 && Number(match[2]) >= 35) colors.push(match[0]);
    }
    if (/\b(?:color|background|fill|stroke)\s*:\s*(?:yellow|orange|gold|amber)\b/i.test(line)) {
      colors.push("named warm color");
    }
    if (colors.length) failures.push(`${relative(root, path)}:${index + 1}: ${colors.join(", ")}`);
  });
}

function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path);
    else if ([".ts", ".tsx", ".css"].includes(extname(entry.name)) && !entry.name.includes(".test.")) scanFile(path);
  }
}

walk(root);
if (failures.length) {
  console.error("Yellow/orange UI colors are prohibited. Use semantic soft-red or blue tokens:\n" + failures.join("\n"));
  process.exitCode = 1;
} else {
  console.log("No literal yellow/orange UI colors found.");
}
