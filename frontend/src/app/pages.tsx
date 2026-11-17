import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { ServiceStatus } from "../features/status/ServiceStatus";

export function HomePage() {
  return (
    <section aria-labelledby="home-title" className="max-w-2xl">
      <h1 id="home-title" className="text-2xl font-medium">
        Understand any repository through cited, browsable explanations
      </h1>
      <p className="mt-3 text-base leading-[26px] text-muted">
        This is the M0 foundation build. Sign-in, workspaces and private builds arrive in M1.
      </p>
      <div className="mt-6">
        <ServiceStatus />
      </div>
    </section>
  );
}

export function NotFoundPage() {
  return (
    <section aria-labelledby="nf-title">
      <h1 id="nf-title" className="text-2xl font-medium">
        Page not found
      </h1>
      <p className="mt-3 text-muted">
        <Link to="/" className="text-primary underline underline-offset-2">
          Go to the start page
        </Link>
      </p>
    </section>
  );
}

export function RouteError() {
  const error = useRouteError();
  const title = isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : "Something went wrong";
  return (
    <main id="main" className="mx-auto max-w-2xl px-6 py-8">
      <h1 className="text-2xl font-medium">{title}</h1>
      <p className="mt-3 text-muted">Reload the page or return to the start page.</p>
    </main>
  );
}
