import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { PageHeader } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { authKeys, fetchCurrentUser } from "../../lib/api/auth";
import { api } from "../../lib/api/resources";
import { safeReturnPath } from "../../lib/returnPath";

export const HANDLE_PATTERN = "[a-z0-9]+(-[a-z0-9]+)*";
export const HANDLE_HINT = "1-39 lowercase letters, digits and single hyphens. It appears in your sites' addresses.";

/** Asked once after the first sign-in (0038): confirm the suggested name or choose another. */
export function WelcomePage() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const user = useQuery({ queryKey: authKeys.me, queryFn: ({ signal }) => fetchCurrentUser(signal), retry: false });
  const [handle, setHandle] = useState<string | null>(null);
  const value = handle ?? user.data?.handle ?? "";
  const save = useMutation({
    mutationFn: () => api.setHandle(value.trim()),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: authKeys.me });
      void navigate(safeReturnPath(params.get("return_to")), { replace: true });
    },
  });
  return (
    <section aria-labelledby="welcome-title" className="max-w-xl">
      <PageHeader
        title={<span id="welcome-title">Choose your name</span>}
        description="Your sites are published at /sites/<your name>/<project>. We suggested one; keep it or pick another. You can change it later, and an old name stays reserved for you for 90 days."
      />
      <form
        className="flex flex-col gap-4 sm:flex-row sm:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Your name" hint={HANDLE_HINT} className="flex-1">
          <Input
            required
            maxLength={39}
            pattern={HANDLE_PATTERN}
            value={value}
            onChange={(e) => setHandle(e.target.value.toLowerCase())}
            autoComplete="username"
          />
        </Field>
        <Button type="submit" variant="primary" disabled={save.isPending || !value.trim()}>
          Continue
        </Button>
      </form>
      {save.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {save.error instanceof ApiError ? save.error.message : "That did not work. Try again."}
        </p>
      ) : null}
    </section>
  );
}
