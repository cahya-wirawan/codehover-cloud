import { useSearchParams } from "react-router";

import { signInUrl, type Provider } from "../../lib/api/auth";

// One message per error code the callback can return (02, S02).
const ERRORS: Record<string, string> = {
  denied: "Sign-in was cancelled at the provider.",
  expired: "That sign-in link expired or was already used. Please try again.",
  provider: "The sign-in provider could not confirm your identity. Please try again.",
  unavailable: "This sign-in option is not available right now.",
};

const PROVIDERS: { id: Provider; label: string }[] = [
  { id: "google", label: "Continue with Google" },
  { id: "github", label: "Continue with GitHub" },
];

export function LoginPage() {
  const [params] = useSearchParams();
  const error = params.get("error");
  const returnTo = params.get("return_to") ?? "/";
  const message = error ? (ERRORS[error] ?? ERRORS.provider) : null;

  return (
    <section aria-labelledby="login-title" className="mx-auto max-w-sm">
      <h1 id="login-title" className="text-2xl font-medium">
        Sign in
      </h1>
      <p className="mt-2 text-muted">Use your Google or GitHub account. Repository access is granted separately.</p>
      {message ? (
        <p role="alert" className="mt-4 rounded-lg border border-border bg-surface p-3 text-destructive">
          {message}
        </p>
      ) : null}
      <ul className="mt-6 flex flex-col gap-3">
        {PROVIDERS.map((provider) => (
          <li key={provider.id}>
            <a
              href={signInUrl(provider.id, returnTo)}
              className="flex h-10 items-center justify-center rounded-lg border border-input-border bg-surface font-medium hover:bg-surface-muted"
            >
              {provider.label}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
