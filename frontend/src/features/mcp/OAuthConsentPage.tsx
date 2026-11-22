import { useMutation, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";

import { Button } from "../../components/ui/button";
import { ApiError } from "../../lib/api/client";
import { api } from "../../lib/api/resources";

const ERRORS: Record<string, string> = {
  invalid_client: "This app is not registered with codehover, or it asked to return somewhere it did not register.",
  rate_limited: "Too many sign-in attempts. Wait a minute, then start again from your app.",
};

/** MCP sign-in consent (0037): an MCP client asks to read one private site as the signed-in person. The
 * request came from the app's own authorize endpoint; approving sends the browser back to the client. */
export function OAuthConsentPage() {
  const [params] = useSearchParams();
  const requestId = params.get("request");
  const error = params.get("error");
  const consent = useQuery({
    queryKey: ["oauth-request", requestId],
    queryFn: () => api.oauthConsent(requestId ?? ""),
    enabled: Boolean(requestId) && !error,
    retry: false,
  });
  const decide = useMutation({
    mutationFn: (approve: boolean) => api.oauthDecide(requestId ?? "", approve),
    onSuccess: ({ redirect_to }) => window.location.assign(redirect_to),
  });

  if (error || !requestId) {
    return (
      <section aria-labelledby="oauth-title" className="max-w-xl">
        <h1 id="oauth-title" className="text-xl font-semibold">
          Sign-in request not accepted
        </h1>
        <p role="alert" className="mt-3 text-muted">
          {ERRORS[error ?? ""] ?? "This sign-in link is incomplete. Start again from your app."}
        </p>
      </section>
    );
  }
  if (consent.isPending) {
    return (
      <p role="status" className="text-muted">
        Loading the request…
      </p>
    );
  }
  if (consent.isError) {
    return (
      <section aria-labelledby="oauth-title" className="max-w-xl">
        <h1 id="oauth-title" className="text-xl font-semibold">
          Sign-in request not available
        </h1>
        <p role="alert" className="mt-3 text-muted">
          {consent.error instanceof ApiError ? consent.error.message : "The request could not be loaded."}
        </p>
      </section>
    );
  }
  const request = consent.data;
  return (
    <section aria-labelledby="oauth-title" className="max-w-xl">
      <h1 id="oauth-title" className="text-xl font-semibold">
        Allow {request.client_name} to read {request.project}?
      </h1>
      <p className="mt-3">
        {request.client_name} will read the published site <span className="font-mono">{request.slug}</span> of{" "}
        {request.workspace} as you: its explanations, guide and source code. It can read nothing else, and it
        cannot change anything.
      </p>
      <p className="mt-2 text-muted">
        After you allow it, you return to <span className="font-mono">{request.redirect_host}</span>. Only continue
        if you started this from your own app. You can disconnect it at any time on the workspace page.
      </p>
      <div className="mt-5 flex gap-3">
        <Button onClick={() => decide.mutate(true)} disabled={decide.isPending}>
          Allow
        </Button>
        <Button variant="secondary" onClick={() => decide.mutate(false)} disabled={decide.isPending}>
          Deny
        </Button>
      </div>
      {decide.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {decide.error instanceof ApiError ? decide.error.message : "That did not work. Try again."}
        </p>
      ) : null}
    </section>
  );
}
