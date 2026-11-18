import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

/** The token arrives in the URL fragment, which browsers never send to the server (no logs, no Referer). */
export function AcceptInvitationPage() {
  const navigate = useNavigate();
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("token"));
  const [failure, setFailure] = useState<string | null>(null);
  const started = useRef(false);
  const error = token ? failure : "This invitation link is incomplete.";

  useEffect(() => {
    if (started.current || !token) return;
    started.current = true;
    api.acceptInvitation(token).then(
      ({ workspace_id }) => navigate(`/w/${workspace_id}`, { replace: true }),
      (reason: unknown) =>
        setFailure(reason instanceof ApiError ? reason.message : "The invitation could not be accepted."),
    );
  }, [navigate, token]);

  return (
    <section aria-labelledby="inv-title" className="max-w-xl">
      <h1 id="inv-title" className="text-2xl font-medium">Joining workspace</h1>
      {error ? <p role="alert" className="mt-3 text-destructive">{error}</p> : <p role="status" className="mt-3 text-muted">Checking your invitation…</p>}
    </section>
  );
}
