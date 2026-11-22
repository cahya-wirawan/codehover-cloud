/** A same-origin path to return to after a step such as sign-in or choosing a name; anything else is "/". */
export function safeReturnPath(candidate: string | null | undefined): string {
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//") || candidate.includes("\\")) return "/";
  return candidate;
}
