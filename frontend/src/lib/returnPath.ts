/** A same-origin path to return to after a step such as sign-in or choosing a name; anything else is
 * "/". The candidate is resolved like the browser would and must stay on this origin (security review:
 * no parser differentials such as "/\\evil" or control characters). */
export function safeReturnPath(candidate: string | null | undefined): string {
  // eslint-disable-next-line no-control-regex
  if (!candidate || !candidate.startsWith("/") || /[\x00-\x20\\]/.test(candidate)) return "/";
  try {
    const url = new URL(candidate, window.location.origin);
    if (url.origin !== window.location.origin) return "/";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/";
  }
}
