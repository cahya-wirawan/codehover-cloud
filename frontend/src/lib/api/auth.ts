import { ApiError, apiGet, apiSend, resetCsrfToken } from "./client";

export interface CurrentUser {
  id: string;
  display_name: string;
  primary_email: string | null;
  operator: boolean;
}

export type Provider = "google" | "github";

export const authKeys = { me: ["auth", "me"] as const };

function parseUser(value: unknown): CurrentUser {
  const v = value as Partial<CurrentUser>;
  if (typeof v.id !== "string" || typeof v.display_name !== "string") throw new Error("Unexpected /me response");
  return {
    id: v.id,
    display_name: v.display_name,
    primary_email: typeof v.primary_email === "string" ? v.primary_email : null,
    operator: v.operator === true,
  };
}

/** The signed-in user, or null when signed out (401). Other errors propagate. */
export async function fetchCurrentUser(signal?: AbortSignal): Promise<CurrentUser | null> {
  try {
    return await apiGet("/me", parseUser, signal);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export async function signOut(): Promise<void> {
  await apiSend("POST", "/auth/logout");
  resetCsrfToken();
}

/** Full-page navigation to the provider; the server validates `return_to` again. */
export function signInUrl(provider: Provider, returnTo: string): string {
  const safe = returnTo.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/";
  return `/api/v1/auth/${provider}/start?${new URLSearchParams({ return_to: safe }).toString()}`;
}
