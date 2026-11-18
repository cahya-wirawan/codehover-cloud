import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";

import { authKeys, fetchCurrentUser, signOut } from "../../lib/api/auth";

export function AccountMenu() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: user, isPending } = useQuery({
    queryKey: authKeys.me,
    queryFn: ({ signal }) => fetchCurrentUser(signal),
  });
  const logout = useMutation({
    mutationFn: signOut,
    onSuccess: async () => {
      // Drop all authorized data from memory before leaving (10).
      queryClient.clear();
      await navigate("/login");
    },
  });

  if (isPending) return null;
  if (!user) {
    return (
      <Link to="/login" className="font-medium text-primary">
        Sign in
      </Link>
    );
  }
  return (
    <div className="flex items-center gap-3">
      {user.operator ? <Link to="/ops" className="text-muted underline">Operations</Link> : null}
      <Link to="/account" className="text-muted underline">{user.display_name}</Link>
      <button
        type="button"
        onClick={() => logout.mutate()}
        disabled={logout.isPending}
        className="h-8 rounded-lg border border-input-border px-3 font-medium hover:bg-surface-muted disabled:opacity-60"
      >
        Sign out
      </button>
    </div>
  );
}
