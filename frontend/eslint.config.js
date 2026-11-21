import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "dist-export", "coverage", "node_modules"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.strict],
    languageOptions: { ecmaVersion: 2022, globals: globals.browser },
  },
  reactHooks.configs.flat.recommended,
  { files: ["src/**/*.tsx"], ...reactRefresh.configs.vite },
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      // Secrets never come from the client environment; see scripts/check-env-usage.mjs.
      "no-restricted-properties": ["error", { object: "process", property: "env" }],
    },
  },
  { files: ["scripts/**/*.mjs", "*.config.{js,ts}"], languageOptions: { globals: globals.node } },
);
