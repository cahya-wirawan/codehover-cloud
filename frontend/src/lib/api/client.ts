/** Same-origin API client. The base path is public configuration, never a secret (22). */
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api/v1";

/** Error envelope from 06_API_SPEC.md "Shared behavior". */
export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string | null;
  retryable: boolean;
  details: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryable: boolean;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.requestId = body.request_id;
    this.retryable = body.retryable;
  }
}

function isErrorEnvelope(value: unknown): value is { error: ApiErrorBody } {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const error = (value as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as ApiErrorBody).code === "string" &&
    typeof (error as ApiErrorBody).message === "string"
  );
}

async function parseError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON error (for example a gateway page): fall through to a generic envelope.
  }
  if (isErrorEnvelope(body)) return new ApiError(response.status, body.error);
  return new ApiError(response.status, {
    code: response.status >= 500 ? "UNAVAILABLE" : "ERROR",
    message: "The service could not complete the request.",
    request_id: response.headers.get("x-request-id"),
    retryable: response.status >= 500,
    details: {},
  });
}

export async function apiGet<T>(
  path: string,
  parse: (value: unknown) => T,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    ...(signal ? { signal } : {}),
  });
  if (!response.ok) throw await parseError(response);
  return parse(await response.json());
}

let csrfToken: string | null = null;

/** Forget the session-bound CSRF token (after sign-out or a 403 from a stale token). */
export function resetCsrfToken(): void {
  csrfToken = null;
}

async function getCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  const token = await apiGet("/auth/csrf", (value) => {
    const candidate = (value as { csrf_token?: unknown }).csrf_token;
    if (typeof candidate !== "string") throw new Error("Unexpected CSRF response");
    return candidate;
  });
  csrfToken = token;
  return token;
}

/** Unsafe request: the browser adds Origin; the session-bound X-CSRF-Token comes from /auth/csrf (09). */
export async function apiSend(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<Response> {
  const token = await getCsrfToken();
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "X-CSRF-Token": token,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    if (response.status === 403) resetCsrfToken();
    throw await parseError(response);
  }
  return response;
}
