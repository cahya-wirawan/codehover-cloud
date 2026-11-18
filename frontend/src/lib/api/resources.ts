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
export interface BuildUsage {
  cap_micro_usd: number;
  reserved_micro_usd: number;
  settled_micro_usd: number;
}
export interface Build extends BuildSummary {
  project_id: string;
  snapshot_id: string;
  estimate_id: string | null;
  retry_of_id: string | null;
  cancel_requested_at: string | null;
  prompt_version: string;
  snapshot_status: string;
  rejection_code: string | null;
  stages: { stage: string; state: string; error_code: string | null }[];
  counts: { files: number; symbols: number };
  usage: BuildUsage;
}
export interface Snapshot {
  id: string;
  commit_sha: string;
  observed_ref: string;
  status: "pending" | "acquiring" | "scanned" | "rejected" | "failed";
  rejection_code: string | null;
  scan_summary: Record<string, number>;
}
export interface Preset {
  name: string;
  description: string;
  writer_model: string;
  checker_model: string;
  providers: string[];
  data_regions: string[];
}
export interface GenerationConfig {
  model_preset: string;
  include: string[];
  exclude: string[];
  max_files: number;
  guide_pages: number;
}
export interface Estimate {
  id: string;
  snapshot_id: string;
  commit_sha: string;
  price_version: string;
  selected_files: number;
  excluded_files: number;
  estimated_tokens: { low: { input: number; output: number }; high: { input: number; output: number } };
  cost_micro_usd: { low: number; high: number };
  expires_at: string;
  expired: boolean;
  disclaimer: string;
}
export interface Usage {
  period_start: string;
  period_end: string;
  allowance_micro_usd: number;
  reserved_micro_usd: number;
  settled_micro_usd: number;
  available_micro_usd: number;
}
export type CheckStatus = "supported" | "partial" | "unsupported" | "unchecked" | "invalid";
export interface GuidePageSummary {
  slug: string;
  title: string;
  ordinal: number;
  purpose: string;
  status: "planned" | "written" | "failed";
  check_status: CheckStatus;
  provenance: Record<string, unknown>;
}
export interface GuideIndex {
  overview: {
    overview: string;
    status: string;
    key_concepts: { term: string; explanation: string }[];
    reading_order: { file_id: string; reason: string }[];
  } | null;
  pages: GuidePageSummary[];
}
export interface PageClaim {
  claim_key: string;
  text: string;
  status: CheckStatus;
  reason: string | null;
  waived: boolean;
}
export interface GuidePage extends GuidePageSummary {
  markdown: string;
  citations: { file_id: string; path: string | null; start_line: number; end_line: number }[];
  claims: PageClaim[];
}
export interface Quality {
  claims: number;
  counts: Record<CheckStatus, number>;
  waived: { unsupported: number; partial: number };
  supported_fraction: number | null;
  uncited_sentences: number;
  pages: { total: number; failed: number };
  publishable: boolean;
  blockers: string[];
  requires_acknowledgment: boolean;
}

/** Integer USD micro-units to a display string, rounded up to the cent; no float arithmetic on stored amounts. */
export function formatUsd(micro: number): string {
  const cents = Math.ceil(micro / 10_000);
  const dollars = Math.floor(cents / 100);
  return "$" + dollars.toLocaleString("en-US") + "." + String(cents % 100).padStart(2, "0");
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
  presets: () => apiJson<{ price_version: string; items: Preset[] }>("/catalog/presets"),
  usage: (wid: string) => apiJson<Usage>(`/workspaces/${wid}/usage`),
  requestSnapshot: async (pid: string) => json<Snapshot>(await apiSend("POST", `/projects/${pid}/snapshots`, {})),
  snapshot: (pid: string, sid: string) => apiJson<Snapshot>(`/projects/${pid}/snapshots/${sid}`),
  createEstimate: async (pid: string, snapshotId: string, config: GenerationConfig) =>
    json<Estimate>(
      await apiSend(
        "POST",
        `/projects/${pid}/estimates`,
        { snapshot_id: snapshotId, config },
        { "Idempotency-Key": crypto.randomUUID() },
      ),
    ),
  startFromEstimate: async (pid: string, estimateId: string, capMicroUsd: number | null) =>
    json<Build>(
      await apiSend(
        "POST",
        `/projects/${pid}/builds`,
        capMicroUsd === null ? { estimate_id: estimateId } : { estimate_id: estimateId, cap_micro_usd: capMicroUsd },
        { "Idempotency-Key": crypto.randomUUID() },
      ),
    ),
  cancelBuild: async (bid: string) => json<Build>(await apiSend("POST", `/builds/${bid}/cancel`, {})),
  retryBuild: async (bid: string) =>
    json<Build>(await apiSend("POST", `/builds/${bid}/retry`, {}, { "Idempotency-Key": crypto.randomUUID() })),
  guide: (bid: string) => apiJson<GuideIndex>(`/builds/${bid}/guide`),
  guidePage: (bid: string, slug: string) => apiJson<GuidePage>(`/builds/${bid}/guide/${encodeURIComponent(slug)}`),
  quality: (bid: string) => apiJson<Quality>(`/builds/${bid}/quality`),
  tree: (bid: string, path: string) =>
    apiJson<Page<TreeEntry>>(`/builds/${bid}/tree?path=${encodeURIComponent(path)}`),
  file: (bid: string, fid: string) => apiJson<FileView>(`/builds/${bid}/files/${fid}`),
  symbol: (bid: string, sid: string) => apiJson<SymbolView>(`/builds/${bid}/symbols/${sid}`),
  acceptInvitation: async (token: string) =>
    json<{ workspace_id: string }>(await apiSend("POST", "/invitations/accept", { token })),
};
