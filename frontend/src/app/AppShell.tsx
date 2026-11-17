import { Link, Outlet } from "react-router";

export function AppShell() {
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:rounded-lg focus:bg-surface focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <header className="flex h-16 items-center border-b border-border bg-surface px-6">
        <Link to="/" className="text-base font-medium">
          codehover-cloud
        </Link>
      </header>
      <main id="main" className="mx-auto max-w-[1440px] px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
