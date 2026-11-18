import { Trash2 } from "lucide-react";
import { type ReactNode, useState } from "react";

import { ApiError } from "../lib/api/client";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Section } from "./ui/section";

/** Deletion with a typed confirmation (02 S19, 03 "Export and deletion"): the button stays disabled until
 * the exact text is typed; it names the specific thing and its consequence (17); errors are announced. */
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
  return (
    <Section title={title} tone="danger" className="mt-10" description={children}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (typed === expected) onConfirm();
        }}
      >
        <label className="flex max-w-md flex-col gap-1.5">
          <span className="text-[13px] font-medium">
            Type {expectedLabel} <strong className="font-mono">{expected}</strong> to confirm
          </span>
          <Input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" />
        </label>
        <div>
          <Button type="submit" variant="destructive" disabled={pending || typed !== expected}>
            <Trash2 aria-hidden="true" />
            {actionLabel}
          </Button>
        </div>
        {error ? (
          <p role="alert" className="text-destructive">
            {error instanceof ApiError ? error.message : "Something went wrong; nothing was deleted."}
          </p>
        ) : null}
      </form>
    </Section>
  );
}
