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
  const here = `${location.pathname}${location.search}`;
  if (user.data === null) {
    return <Navigate to={`/login?${new URLSearchParams({ return_to: here }).toString()}`} replace />;
  }
  if (user.data && !user.data.handle_confirmed && location.pathname !== "/welcome") {
    // 0038: a new person confirms or chooses their name once, then continues where they were going.
    return <Navigate to={`/welcome?${new URLSearchParams({ return_to: here }).toString()}`} replace />;
  }
  return <Outlet />;
}
