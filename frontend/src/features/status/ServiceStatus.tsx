import { useQuery } from "@tanstack/react-query";

import { ApiError } from "../../lib/api/client";
import { fetchLiveStatus, healthKeys } from "../../lib/api/health";

/** API reachability and version. Status is conveyed in words as well as color (17). */
export function ServiceStatus() {
  const { data, error, isPending } = useQuery({
    queryKey: healthKeys.live,
    queryFn: ({ signal }) => fetchLiveStatus(signal),
  });

  if (isPending) {
    return (
      <p role="status" aria-busy="true" className="text-muted">
        Checking the API…
      </p>
    );
  }

  if (error) {
    const requestId = error instanceof ApiError ? error.requestId : null;
    return (
      <div role="alert" className="rounded-lg border border-border bg-surface p-4">
        <p className="font-medium text-destructive">API unavailable</p>
        <p className="mt-1 text-muted">
          The service did not respond as expected.
          {requestId ? (
            <>
              {" "}
              Reference <code className="font-mono">{requestId}</code>.
            </>
          ) : null}
        </p>
      </div>
    );
  }

  return (
    <p role="status" className="flex items-center gap-2">
      <span aria-hidden="true" className="inline-block size-2 rounded-full bg-success" />
      <span>
        API online · version <span className="font-mono">{data.version}</span>
      </span>
    </p>
  );
}
