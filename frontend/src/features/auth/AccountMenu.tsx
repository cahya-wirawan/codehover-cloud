import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, LogOut, Monitor, Moon, Sun, UserRound } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { Button } from "../../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../../components/ui/dropdown-menu";
import { authKeys, fetchCurrentUser, signOut } from "../../lib/api/auth";
import { readTheme, storeTheme, type Theme } from "../../lib/theme";

export function AccountMenu() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [theme, setTheme] = useState<Theme>(readTheme);
  const { data: user, isPending } = useQuery({
    queryKey: authKeys.me,
    queryFn: ({ signal }) => fetchCurrentUser(signal),
  });
  const logout = useMutation({
    mutationFn: signOut,
    onSuccess: async () => {
      queryClient.clear(); // drop all authorized data from memory before leaving (10)
      await navigate("/login");
    },
  });
  const choose = (next: Theme) => {
    setTheme(next);
    storeTheme(next);
  };

  if (isPending) return null;
  if (!user) {
    return (
      <Button asChild variant="primary" size="sm">
        <Link to="/login">Sign in</Link>
      </Button>
    );
  }
  const initials = user.display_name
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex h-9 items-center gap-2 rounded-full border border-border bg-surface py-1 pr-3 pl-1 hover:bg-surface-muted"
        >
          <span aria-hidden="true" className="grid size-7 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
            {initials || "?"}
          </span>
          <span className="hidden max-w-40 truncate text-[13px] font-medium sm:inline">{user.display_name}</span>
          <span className="sr-only">Account menu</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuLabel>
          <span className="block truncate font-medium text-foreground">{user.display_name}</span>
          {user.primary_email ? <span className="block truncate">{user.primary_email}</span> : null}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/account">
            <UserRound aria-hidden="true" />
            Account
          </Link>
        </DropdownMenuItem>
        {user.operator ? (
          <DropdownMenuItem asChild>
            <Link to="/ops">
              <Activity aria-hidden="true" />
              Operations
            </Link>
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        {(
          [
            ["light", "Light", <Sun key="i" aria-hidden="true" />],
            ["dark", "Dark", <Moon key="i" aria-hidden="true" />],
            ["system", "Match system", <Monitor key="i" aria-hidden="true" />],
          ] as const
        ).map(([value, label, icon]) => (
          <DropdownMenuItem key={value} onSelect={() => choose(value)} aria-checked={theme === value} role="menuitemradio">
            {icon}
            {label}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => logout.mutate()} disabled={logout.isPending}>
          <LogOut aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
