import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

const PENDING = "codehover.invitation";

/** Session storage can be unavailable (private windows, blocked site data): then a signed-out visitor
 * signs in and opens the link again. */
function stash(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(PENDING, token);
    else sessionStorage.removeItem(PENDING);
  } catch {
    // ignored: see above
  }
}

function stashed(): string | null {
  try {
    return sessionStorage.getItem(PENDING);
  } catch {
    return null;
  }
}

/** The token arrives in the URL fragment, which browsers never send to the server (no logs, no Referer).
 * A signed-out visitor is sent to sign in first; the token waits in this tab's session storage, never in
 * the sign-in URL, and is used once on the way back. */
export function AcceptInvitationPage() {
  const navigate = useNavigate();
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("token") ?? stashed());
  const [failure, setFailure] = useState<string | null>(null);
  const started = useRef(false);
  const error = token ? failure : "This invitation link is incomplete.";

  useEffect(() => {
    if (started.current || !token) return;
    started.current = true;
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
    api.acceptInvitation(token).then(
      ({ workspace_id }) => {
        stash(null);
        void navigate(`/w/${workspace_id}`, { replace: true });
      },
      (reason: unknown) => {
        if (reason instanceof ApiError && reason.status === 401) {
          stash(token);
          void navigate(`/login?${new URLSearchParams({ return_to: "/invitations/accept" }).toString()}`, { replace: true });
          return;
        }
        stash(null);
        setFailure(reason instanceof ApiError ? reason.message : "The invitation could not be accepted.");
      },
    );
  }, [navigate, token]);

  return (
    <section aria-labelledby="inv-title" className="max-w-xl">
      <h1 id="inv-title" className="text-[22px] leading-8 font-semibold">Joining workspace</h1>
      {error ? <p role="alert" className="mt-3 text-destructive">{error}</p> : <p role="status" className="mt-3 text-muted">Checking your invitation…</p>}
    </section>
  );
}
