import { useQuery } from "@tanstack/react-query";
import { Navigate, Outlet, useLocation } from "react-router";

import { authKeys, fetchCurrentUser } from "../lib/api/auth";

/** Workspace, project, account and operations pages need a signed-in user: a signed-out visitor goes to the
 * login page, which brings them back here afterwards. Errors other than "signed out" render the page,
 * whose own requests then show the error. */
export function RequireSignIn() {
  const location = useLocation();
  const user = useQuery({ queryKey: authKeys.me, queryFn: ({ signal }) => fetchCurrentUser(signal), retry: false });
  if (user.isPending) {
    return (
      <p role="status" className="text-muted">
        Checking your sign-in…
      </p>
    );
  }
  if (user.data === null) {
    const here = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?${new URLSearchParams({ return_to: here }).toString()}`} replace />;
  }
  return <Outlet />;
}
