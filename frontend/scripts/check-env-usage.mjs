#!/usr/bin/env node
// M0 exit criterion "no secret in frontend" (14, 22).
// 1. Source may read only allowlisted import.meta.env keys (public configuration).
// 2. With CANARY_SECRETS="v1,v2" set during `npm run build`, none of those values may appear in dist/.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const ALLOWED = new Set(["VITE_API_BASE", "MODE", "DEV", "PROD", "BASE_URL", "SSR"]);
const root = new URL("..", import.meta.url).pathname;

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else yield path;
  }
}

const problems = [];
for (const path of files(join(root, "src"))) {
  if (!/\.(ts|tsx|js|jsx)$/.test(path)) continue;
  const text = readFileSync(path, "utf8");
  for (const match of text.matchAll(/import\.meta\.env\.([A-Za-z0-9_]+)/g)) {
    if (!ALLOWED.has(match[1])) problems.push(`${path}: import.meta.env.${match[1]} is not allowlisted`);
  }
  if (/import\.meta\.env\s*\[/.test(text) || /\.\.\.\s*import\.meta\.env\b/.test(text)) {
    problems.push(`${path}: dynamic access to import.meta.env is not allowed`);
  }
}

const canaries = (process.env.CANARY_SECRETS ?? "").split(",").map((v) => v.trim()).filter(Boolean);
if (canaries.length > 0) {
  const dist = join(root, "dist");
  for (const path of files(dist)) {
    const text = readFileSync(path, "latin1");
    for (const canary of canaries) {
      if (text.includes(canary)) problems.push(`${path}: contains a canary secret value`);
    }
  }
}

if (problems.length > 0) {
  for (const p of problems) console.error(`FAIL ${p}`);
  process.exit(1);
}
console.log(`frontend env check OK (${canaries.length} canary value(s) checked in dist)`);
