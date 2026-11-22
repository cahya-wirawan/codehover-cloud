import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Field, Input } from "../../components/ui/input";
import { Section } from "../../components/ui/section";
import { ApiError } from "../../lib/api/client";
import { api, type Project } from "../../lib/api/resources";

const PATTERN = "[a-z0-9]([a-z0-9\\-]{0,46}[a-z0-9])?";

/** The project's address, the <project> of /sites/<owner>/<project> (0041). Admins change it; the old
 * address keeps leading to the site for 90 days. */
export function AddressPanel({ project }: { project: Project }) {
  const client = useQueryClient();
  const [slug, setSlug] = useState(project.slug);
  const owner = project.address?.split("/")[0] ?? "";
  const save = useMutation({
    mutationFn: () => api.changeProjectAddress(project.id, slug.trim()),
    onSuccess: (updated) => {
      client.setQueryData(["project", project.id], updated);
      void client.invalidateQueries({ queryKey: ["publication", project.id] });
      void client.invalidateQueries({ queryKey: ["projects", project.workspace_id] });
    },
  });
  return (
    <Section
      title="Address"
      description="Where this project's site lives once published. After a change, the old address keeps leading to the site for 90 days."
    >
      <form
        className="flex max-w-xl flex-col gap-4 sm:flex-row sm:items-end"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Project address" hint={`The site is /sites/${owner || "<owner>"}/${slug || "<address>"}.`} className="flex-1">
          <Input required maxLength={48} pattern={PATTERN} value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} />
        </Field>
        <Button type="submit" disabled={save.isPending || slug === project.slug || !slug.trim()}>
          Change address
        </Button>
      </form>
      {save.error ? (
        <p role="alert" className="mt-3 text-destructive">
          {save.error instanceof ApiError ? save.error.message : "That did not work. Try again."}
        </p>
      ) : null}
    </Section>
  );
}
