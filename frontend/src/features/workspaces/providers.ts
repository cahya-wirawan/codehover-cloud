import type { KeyProvider } from "../../lib/api/resources";

/** The model providers a workspace can hold its own key for, in display order (07 BYOK). */
export const KEY_PROVIDERS: { id: KeyProvider; name: string }[] = [
  { id: "anthropic", name: "Anthropic" },
  { id: "openai", name: "OpenAI" },
  { id: "gemini", name: "Gemini" },
  { id: "openrouter", name: "OpenRouter" },
];

/** "own Anthropic key", "own OpenAI and Gemini keys": the keys a preset's paid providers need. */
export function ownKeysLabel(providers: string[]): string {
  const names = KEY_PROVIDERS.filter((p) => providers.includes(p.id)).map((p) => p.name);
  if (names.length <= 1) return `own ${names[0] ?? "provider"} key`;
  return `own ${names.slice(0, -1).join(", ")} and ${names[names.length - 1]} keys`;
}
