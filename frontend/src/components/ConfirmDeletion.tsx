import { type ReactNode, useId, useState } from "react";

import { ApiError } from "../lib/api/client";

/** Deletion with a typed confirmation (02 S19, 03 "Export and deletion"): the button stays disabled
 * until the exact text is typed; errors are announced. */
export function ConfirmDeletion({
  title,
  expected,
  expectedLabel,
  actionLabel,
  pending,
  error,
  onConfirm,
  children,
}: {
  title: string;
  expected: string;
  expectedLabel: string;
  actionLabel: string;
  pending: boolean;
  error: unknown;
  onConfirm: () => void;
  children: ReactNode;
}) {
  const [typed, setTyped] = useState("");
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="mt-8 rounded-lg border border-destructive p-4">
      <h2 id={headingId} className="text-lg font-medium">{title}</h2>
      <div className="mt-1 space-y-1">{children}</div>
      <form
        className="mt-3 space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (typed === expected) onConfirm();
        }}
      >
        <label className="flex max-w-md flex-col gap-1">
          <span>
            Type {expectedLabel} <strong className="font-mono">{expected}</strong> to confirm
          </span>
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoComplete="off"
            className="h-9 rounded-lg border border-input-border bg-surface px-2"
          />
        </label>
        <button
          type="submit"
          disabled={pending || typed !== expected}
          className="h-10 rounded-lg border border-destructive px-4 font-medium text-destructive disabled:opacity-60"
        >
          {actionLabel}
        </button>
        {error ? (
          <p role="alert" className="text-destructive">
            {error instanceof ApiError ? error.message : "Something went wrong; nothing was deleted."}
          </p>
        ) : null}
      </form>
    </section>
  );
}
