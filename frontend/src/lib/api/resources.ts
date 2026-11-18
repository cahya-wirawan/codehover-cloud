import { apiJson, apiSend } from "./client";

export type Role = "viewer" | "editor" | "admin" | "owner";
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export interface Workspace {
  id: string;
  name: string;
  slug: string;
  role: Role;
}
export interface Connection {
  id: string;
  installation_id: number;
  account: string;
  status: "active" | "suspended" | "revoked";
}
export interface Repository {
  id: string;
  owner: string;
  name: string;
  default_branch: string;
}
export interface Project {
  id: string;
  workspace_id: string;
  name: string;
  slug: string;
  tracked_ref: string;
  owner: string;
  repository: string;
}
export interface BuildSummary {
  id: string;
  status: string;
  commit_sha: string;
  created_at: string;
}
export interface Build extends BuildSummary {
  project_id: string;
  snapshot_status: string;
  rejection_code: string | null;
  stages: { stage: string; state: string; error_code: string | null }[];
  counts: { files: number; symbols: number };
}
export interface TreeEntry {
  name: string;
  path: string;
  type: "dir" | "file";
  file_id: string | null;
  status: string | null;
  reason: string | null;
}
export interface FileSymbol {
  id: string;
  name: string;
  kind: string;
  start_line: number;
  end_line: number;
  summary: string | null;
}
export interface FileView {
  id: string;
  path: string;
  start_line: number;
  lines: string[];
  symbols: FileSymbol[];
}
export interface Related {
  id: string;
  name: string;
  kind: string;
  path: string;
  resolution: string;
  ambiguous: boolean;
}
export interface SymbolView {
  id: string;
  name: string;
  kind: string;
  signature: string | null;
  path: string;
  start_line: number;
  card: { summary?: string; details?: string; params?: { name: string; description: string }[]; notes?: string };
  uses: Related[];
  used_by: Related[];
}

const json = async <T>(response: Response) => (await response.json()) as T;
export const TERMINAL = new Set(["ready", "failed", "cancelled", "budget_exhausted"]);

export const api = {
  workspaces: () => apiJson<Page<Workspace>>("/workspaces"),
  createWorkspace: async (name: string, slug: string) => json<Workspace>(await apiSend("POST", "/workspaces", { name, slug })),
  connections: (wid: string) => apiJson<Page<Connection>>(`/workspaces/${wid}/connections`),
  startInstall: async (wid: string) =>
    json<{ install_url: string }>(await apiSend("POST", `/workspaces/${wid}/github/install`)),
  repositories: (wid: string, connectionId: string) =>
    apiJson<Page<Repository>>(`/workspaces/${wid}/repositories?connection_id=${encodeURIComponent(connectionId)}`),
  projects: (wid: string) => apiJson<Page<Project>>(`/workspaces/${wid}/projects`),
  createProject: async (wid: string, body: { repository_id: string; name: string; slug: string }) =>
    json<Project>(await apiSend("POST", `/workspaces/${wid}/projects`, body)),
  project: (pid: string) => apiJson<Project>(`/projects/${pid}`),
  builds: (pid: string) => apiJson<Page<BuildSummary>>(`/projects/${pid}/builds`),
  startBuild: async (pid: string) =>
    json<Build>(await apiSend("POST", `/projects/${pid}/builds`, {}, { "Idempotency-Key": crypto.randomUUID() })),
  build: (bid: string) => apiJson<Build>(`/builds/${bid}`),
  tree: (bid: string, path: string) =>
    apiJson<Page<TreeEntry>>(`/builds/${bid}/tree?path=${encodeURIComponent(path)}`),
  file: (bid: string, fid: string) => apiJson<FileView>(`/builds/${bid}/files/${fid}`),
  symbol: (bid: string, sid: string) => apiJson<SymbolView>(`/builds/${bid}/symbols/${sid}`),
  acceptInvitation: async (token: string) =>
    json<{ workspace_id: string }>(await apiSend("POST", "/invitations/accept", { token })),
};
