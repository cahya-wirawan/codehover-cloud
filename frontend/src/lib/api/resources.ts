import { API_BASE, apiGetWithEtag, apiJson, apiSend, apiUpload } from "./client";

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
  source?: "github" | "upload" | "public";
  access?: "workspace" | "restricted";
  my_role?: Role | null;
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
  language?: string;
  level?: string;
  variant_of?: string | null;
  snapshot_status: string;
  rejection_code: string | null;
  stages: { stage: string; state: string; error_code: string | null }[];
  counts: { files: number; symbols: number };
  usage: BuildUsage;
  content_purged_at?: string | null;
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
  batch?: boolean;
  own_key?: boolean;
}
export interface GenerationConfig {
  model_preset: string;
  include: string[];
  exclude: string[];
  max_files: number;
  guide_pages: number;
  languages?: string[];
  levels?: string[];
  variant_of?: string;
  batch?: boolean;
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
export interface UsageEntry {
  id: string;
  build_id: string | null;
  attempt_id: string | null;
  kind: string;
  amount_micro_usd: number;
  created_at: string;
}
export interface PlanInfo {
  key: "trial" | "pro" | "team";
  name: string;
  allowance_micro_usd: number;
  seats: number;
  projects: number;
  storage_gib: number;
  active_builds: number;
}
export interface BillingState {
  configured: boolean;
  enforced: boolean;
  plan: PlanInfo["key"] | null;
  status: string | null;
  period_end: string | null;
  cancel_at_period_end: boolean;
  trial_ends_at: string | null;
  trial_available: boolean;
  generation_blocked: "past_due" | "disputed" | null;
  hosting_suspended: boolean;
  has_customer: boolean;
  plans: (PlanInfo & { available: boolean })[];
}
export type PushPolicyName = "off" | "snapshot" | "build";
export interface PushPolicy {
  policy: PushPolicyName;
  cap_micro_usd: number;
  set_by_name: string | null;
  version: number;
}
export interface PushRequest {
  id: string;
  commit_sha: string;
  ref: string;
  pushes: number;
  state: "pending" | "acquiring" | "done" | "skipped" | "failed";
  outcome: string | null;
  snapshot_id: string | null;
  build_id: string | null;
  first_received_at: string;
  updated_at: string;
}
export type KeyProvider = "anthropic" | "openai" | "gemini" | "openrouter";
export interface ProviderKey {
  provider: KeyProvider;
  hint: string;
  created_at: string;
  created_by_name: string | null;
  verified_at: string | null;
}
export interface WorkspaceMember {
  user_id: string;
  display_name: string;
  role: Role;
  created_at: string;
}
export interface Invitation {
  id: string;
  email: string;
  role: Role;
  expires_at: string;
}
export interface ProjectAccess {
  mode: "workspace" | "restricted";
  version: number;
  members: { user_id: string; name: string; role: "viewer" | "editor"; workspace_role: Role; created_at: string }[];
}
export interface Variant {
  id: string;
  language: string;
  level: string;
  base: boolean;
}
export const LANGUAGES: Record<string, string> = { en: "English", de: "Deutsch", id: "Bahasa Indonesia" };
export const LEVELS: Record<string, string> = { standard: "Standard", beginner: "Beginner", expert: "Expert" };
export interface SiteMeta {
  operator_name: string;
  support_email: string | null;
  billing_available: boolean;
  trial_days: number;
  plans: PlanInfo[];
}
export type CheckStatus = "supported" | "partial" | "unsupported" | "unchecked" | "invalid";
export interface GuidePageSummary {
  slug: string;
  title: string;
  section: string;
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
  symbols: { mention: string; id: string; name: string; kind: string; file_id: string; start_line: number }[];
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

export interface Publication {
  build_id: string;
  visibility: "private" | "public";
  public_slug: string | null;
  version: number;
  acknowledged_partial: boolean;
  suspended_at: string | null;
  suspended_reason: string | null;
  updated_at: string;
  commit_sha: string;
  /** FR-029: the checked license of a public site of a public-source repository. */
  license_spdx: string | null;
}
export interface PublishRequest {
  build_id: string;
  visibility: "private" | "public";
  public_slug: string | null;
  confirmation: string | null;
  acknowledge_partial: boolean;
}
export interface Site {
  project: string;
  repository: string;
  commit_sha: string;
  visibility: string;
  generated: boolean;
  published_at: string;
  /** FR-029: named on public sites of repositories read through the public source. */
  license: { spdx_id: string; path: string; file_id: string | null } | null;
}

export interface ExportJob {
  id: string;
  build_id: string;
  format: "static_zip" | "kb_json";
  status: "pending" | "ready" | "failed" | "expired";
  byte_count: number | null;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
  expired: boolean;
}

export interface Freshness {
  tracked_ref: string;
  state: "current" | "update_available" | "never_built" | "unknown" | "not_tracked";
  reason: string | null;
  latest_sha: string | null;
  last_observed_sha: string | null;
  last_observed_at: string | null;
  published_sha: string | null;
  latest_ready_sha: string | null;
}
export interface AuditEntry {
  id: string;
  action: string;
  actor_user_id: string | null;
  actor_name: string | null;
  resource_type: string;
  resource_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

/** Where the reading views get their data: a build of a project, or a published site (02 S11-S13, S20). */
export interface ViewerSource {
  key: string;
  tree: (path: string) => Promise<Page<TreeEntry>>;
  file: (fid: string, startLine: number) => Promise<FileView>;
  symbol: (sid: string) => Promise<SymbolView>;
  guide: () => Promise<GuideIndex>;
  guidePage: (slug: string) => Promise<GuidePage>;
  search: (q: string, kind: string | null) => Promise<Page<SearchHit>>;
  quality: (() => Promise<Quality>) | null;
}

function viewerSource(prefix: string, key: string, quality: (() => Promise<Quality>) | null): ViewerSource {
  return {
    key,
    tree: (path) => apiJson<Page<TreeEntry>>(`${prefix}/tree?path=${encodeURIComponent(path)}`),
    file: (fid, startLine) => apiJson<FileView>(`${prefix}/files/${fid}?start_line=${startLine}`),
    symbol: (sid) => apiJson<SymbolView>(`${prefix}/symbols/${sid}`),
    guide: () => apiJson<GuideIndex>(`${prefix}/guide`),
    guidePage: (slug) => apiJson<GuidePage>(`${prefix}/guide/${encodeURIComponent(slug)}`),
    search: (q, kind) => {
      const params = new URLSearchParams({ q });
      if (kind) params.set("kind", kind);
      return apiJson<Page<SearchHit>>(`${prefix}/search?${params.toString()}`);
    },
    quality,
  };
}

export const buildSource = (bid: string): ViewerSource =>
  viewerSource(`/builds/${bid}`, `build:${bid}`, () => apiJson<Quality>(`/builds/${bid}/quality`));
export const siteSource = (slug: string): ViewerSource =>
  viewerSource(`/published/${encodeURIComponent(slug)}`, `site:${slug}`, null);

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
  params: string[];
}
export interface FileReference {
  line: number;
  column: number; // 0-based, in Unicode scalar values
  targets: { id: string; name: string; kind: string }[];
  resolution: string;
  ambiguous: boolean;
}
export interface FileView {
  id: string;
  path: string;
  start_line: number;
  end_line?: number;
  line_count?: number | null;
  lines: string[];
  symbols: FileSymbol[];
  references: FileReference[];
  explanation?: {
    status: string;
    summary: string;
    chunks: { start_line: number; end_line: number; summary: string; status: string; notes: { start_line: number; end_line: number; text: string }[] }[];
  } | null;
}
export interface Related {
  id: string;
  name: string;
  kind: string;
  path: string;
  file_id?: string;
  start_line?: number;
  resolution: string;
  ambiguous: boolean;
}
export interface SearchHit {
  kind: "symbol" | "file" | "guide";
  ref_id: string;
  name: string;
  path: string | null;
  line: number | null;
  symbol_kind: string | null;
  snippet: string;
  basis: "exact_name" | "name_prefix" | "text";
}
export interface QualityCheck {
  id: string;
  slug: string;
  claim_key: string;
  claim_text: string;
  citation: string;
  path: string | null;
  start_line: number;
  end_line: number;
  deterministic: "valid" | "invalid";
  status: CheckStatus;
  reason: string;
  waived: boolean;
  waived_reason: string | null;
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
  guides: { slug: string; title: string }[];
}

export interface DeletionRequest {
  id: string;
  scope: "project" | "workspace" | "account";
  state: "pending";
  requested_at: string;
  purge_after: string;
}
export interface AccountDeletion {
  account: DeletionRequest;
  workspaces: DeletionRequest[];
}
export interface OpsSummary {
  queue: Record<string, number>;
  builds_last_24h: Record<string, number>;
  tasks_by_stage: Record<string, number>;
  builds_paused: boolean;
  paused_reason: string;
  paused_since: string | null;
}
export interface OpsTask {
  id: string;
  stage: string;
  state: string;
  attempts: number;
  lease_generation: number;
  lease_until: string | null;
  next_attempt_at: string | null;
  error_code: string | null;
  updated_at: string;
}
export interface OpsBuild {
  id: string;
  workspace_id: string;
  project_id: string;
  status: string;
  created_at: string;
  completed_at: string | null;
  cap_micro_usd: number;
  reserved_micro_usd: number;
  settled_micro_usd: number;
  tasks: OpsTask[];
  provider_attempts: { purpose: string; state: string; count: number; settled_micro_usd: number; error_codes: string[] }[];
  events: number;
}
export interface Entitlement {
  id: string;
  workspace_id: string;
  allowance_micro_usd: number;
  starts_at: string;
  expires_at: string;
  reason: string;
  revoked_at: string | null;
}
export interface OperatorEvent {
  id: string;
  operator_user_id: string;
  action: string;
  workspace_id: string | null;
  target_id: string | null;
  reason: string;
  created_at: string;
}
export interface SupportGrant {
  id: string;
  granted_by_name: string | null;
  reason: string;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  active: boolean;
}
export const ACCOUNT_CONFIRMATION = "delete my account";

const json = async <T>(response: Response) => (await response.json()) as T;
export const TERMINAL = new Set(["ready", "failed", "cancelled", "budget_exhausted"]);

export const api = {
  workspaces: () => apiJson<Page<Workspace>>("/workspaces"),
  deleteProject: async (pid: string, confirmation: string) =>
    json<DeletionRequest>(await apiSend("POST", `/projects/${pid}/deletion`, { confirmation })),
  deleteWorkspace: async (wid: string, confirmation: string) =>
    json<DeletionRequest>(await apiSend("POST", `/workspaces/${wid}/deletion`, { confirmation })),
  deleteAccount: async (confirmation: string, deleteWorkspaces: string[]) =>
    json<AccountDeletion>(await apiSend("POST", "/me/deletion", { confirmation, delete_workspaces: deleteWorkspaces })),
  exportAccount: async () => (await apiSend("POST", "/me/export")).blob(),
  opsSummary: () => apiJson<OpsSummary>("/ops/summary"),
  opsPause: async (paused: boolean, reason: string) =>
    json<OpsSummary>(await apiSend("POST", "/ops/builds-paused", { paused, reason })),
  opsBuild: (bid: string) => apiJson<OpsBuild>(`/ops/builds/${encodeURIComponent(bid)}`),
  opsSuspend: async (pid: string, reason: string) => {
    await apiSend("POST", `/ops/projects/${encodeURIComponent(pid)}/suspension`, { reason });
  },
  opsReinstate: async (pid: string, reason: string) => {
    await apiSend("DELETE", `/ops/projects/${encodeURIComponent(pid)}/suspension?${new URLSearchParams({ reason }).toString()}`);
  },
  opsEntitlements: (wid: string) => apiJson<Page<Entitlement>>(`/ops/workspaces/${encodeURIComponent(wid)}/entitlements`),
  opsGrant: async (wid: string, body: { allowance_micro_usd: number; days: number; reason: string }) =>
    json<Entitlement>(await apiSend("POST", `/ops/workspaces/${encodeURIComponent(wid)}/entitlements`, body)),
  opsRevokeGrant: async (id: string, reason: string) => {
    await apiSend("DELETE", `/ops/entitlements/${encodeURIComponent(id)}?${new URLSearchParams({ reason }).toString()}`);
  },
  opsEvents: () => apiJson<{ items: OperatorEvent[]; next_cursor: string | null }>("/ops/events"),
  supportGrants: (wid: string) => apiJson<Page<SupportGrant>>(`/workspaces/${wid}/support-access`),
  grantSupport: async (wid: string, hours: number, reason: string) =>
    json<SupportGrant>(await apiSend("POST", `/workspaces/${wid}/support-access`, { hours, reason })),
  revokeSupport: async (wid: string, id: string) => {
    await apiSend("DELETE", `/workspaces/${wid}/support-access/${id}`);
  },
  createWorkspace: async (name: string, slug: string) => json<Workspace>(await apiSend("POST", "/workspaces", { name, slug })),
  connections: (wid: string) => apiJson<Page<Connection>>(`/workspaces/${wid}/connections`),
  startInstall: async (wid: string) =>
    json<{ install_url: string }>(await apiSend("POST", `/workspaces/${wid}/github/install`)),
  repositories: (wid: string, connectionId: string) =>
    apiJson<Page<Repository>>(`/workspaces/${wid}/repositories?connection_id=${encodeURIComponent(connectionId)}`),
  projects: (wid: string) => apiJson<Page<Project>>(`/workspaces/${wid}/projects`),
  createProject: async (
    wid: string,
    body:
      | { repository_id: string; name: string; slug: string }
      | { source: "upload"; name: string; slug: string }
      | { source: "public"; public_repository: string; name: string; slug: string; tracked_ref?: string },
  ) => json<Project>(await apiSend("POST", `/workspaces/${wid}/projects`, body)),
  uploadArchive: async (pid: string, file: Blob) =>
    json<Snapshot>(await apiUpload(`/projects/${pid}/uploads`, file, "application/zip")),
  project: (pid: string) => apiJson<Project>(`/projects/${pid}`),
  builds: (pid: string) => apiJson<Page<BuildSummary>>(`/projects/${pid}/builds`),
  startBuild: async (pid: string) =>
    json<Build>(await apiSend("POST", `/projects/${pid}/builds`, {}, { "Idempotency-Key": crypto.randomUUID() })),
  build: (bid: string) => apiJson<Build>(`/builds/${bid}`),
  presets: (workspaceId?: string) =>
    apiJson<{ price_version: string; items: Preset[] }>(
      workspaceId ? `/catalog/presets?workspace_id=${encodeURIComponent(workspaceId)}` : "/catalog/presets",
    ),
  providerKeys: (wid: string) => apiJson<Page<ProviderKey>>(`/workspaces/${wid}/provider-keys`),
  saveProviderKey: async (wid: string, provider: KeyProvider, apiKey: string) =>
    json<ProviderKey>(await apiSend("PUT", `/workspaces/${wid}/provider-keys/${provider}`, { api_key: apiKey })),
  removeProviderKey: async (wid: string, provider: KeyProvider) => {
    await apiSend("DELETE", `/workspaces/${wid}/provider-keys/${provider}`);
  },
  usage: (wid: string) => apiJson<Usage>(`/workspaces/${wid}/usage`),
  usageEntries: (wid: string) => apiJson<Page<UsageEntry>>(`/workspaces/${wid}/usage/entries?limit=20`),
  billing: (wid: string) => apiJson<BillingState>(`/workspaces/${wid}/billing`),
  checkout: async (wid: string, plan: "pro" | "team") =>
    json<{ url: string }>(
      await apiSend("POST", `/workspaces/${wid}/billing/checkout`, { plan }, { "Idempotency-Key": crypto.randomUUID() }),
    ),
  billingPortal: async (wid: string) => json<{ url: string }>(await apiSend("POST", `/workspaces/${wid}/billing/portal`)),
  startTrial: async (wid: string) => json<BillingState>(await apiSend("POST", `/workspaces/${wid}/billing/trial`)),
  meta: () => apiJson<SiteMeta>("/meta"),
  variants: (bid: string) => apiJson<Page<Variant>>(`/builds/${bid}/variants`),
  members: (wid: string) => apiJson<Page<WorkspaceMember>>(`/workspaces/${wid}/members?limit=100`),
  changeRole: async (wid: string, userId: string, role: Role) => {
    await apiSend("PATCH", `/workspaces/${wid}/members/${userId}`, { role });
  },
  removeMember: async (wid: string, userId: string) => {
    await apiSend("DELETE", `/workspaces/${wid}/members/${userId}`);
  },
  invitations: (wid: string) => apiJson<{ items: Invitation[] }>(`/workspaces/${wid}/invitations`),
  invite: async (wid: string, email: string, role: Role) =>
    json<Invitation & { accept_url: string }>(await apiSend("POST", `/workspaces/${wid}/invitations`, { email, role }, { "Idempotency-Key": crypto.randomUUID() })),
  revokeInvitation: async (wid: string, invitationId: string) => {
    await apiSend("DELETE", `/workspaces/${wid}/invitations/${invitationId}`);
  },
  projectAccess: (pid: string) => apiGetWithEtag<ProjectAccess>(`/projects/${pid}/access`),
  setProjectAccess: async (pid: string, mode: ProjectAccess["mode"], ifMatch: string) =>
    json<ProjectAccess>(await apiSend("PUT", `/projects/${pid}/access`, { mode }, { "If-Match": ifMatch })),
  grantProjectAccess: async (pid: string, userId: string, role: "viewer" | "editor") =>
    json<ProjectAccess>(await apiSend("PUT", `/projects/${pid}/access/members/${userId}`, { role })),
  revokeProjectAccess: async (pid: string, userId: string) => {
    await apiSend("DELETE", `/projects/${pid}/access/members/${userId}`);
  },
  pushPolicy: (pid: string) => apiGetWithEtag<PushPolicy>(`/projects/${pid}/push-policy`),
  setPushPolicy: async (pid: string, policy: PushPolicyName, capMicroUsd: number, ifMatch: string) =>
    json<PushPolicy>(
      await apiSend("PUT", `/projects/${pid}/push-policy`, { policy, cap_micro_usd: capMicroUsd }, { "If-Match": ifMatch }),
    ),
  pushRequests: (pid: string) => apiJson<Page<PushRequest>>(`/projects/${pid}/push-requests`),
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
  startFromEstimate: async (pid: string, estimateId: string, capMicroUsd: number | null, ownKey = false) =>
    json<Build>(
      await apiSend(
        "POST",
        `/projects/${pid}/builds`,
        {
          estimate_id: estimateId,
          ...(capMicroUsd === null ? {} : { cap_micro_usd: capMicroUsd }),
          ...(ownKey ? { funding: "own_key" } : {}),
        },
        { "Idempotency-Key": crypto.randomUUID() },
      ),
    ),
  cancelBuild: async (bid: string) => json<Build>(await apiSend("POST", `/builds/${bid}/cancel`, {})),
  retryBuild: async (bid: string) =>
    json<Build>(await apiSend("POST", `/builds/${bid}/retry`, {}, { "Idempotency-Key": crypto.randomUUID() })),
  guide: (bid: string) => apiJson<GuideIndex>(`/builds/${bid}/guide`),
  guidePage: (bid: string, slug: string) => apiJson<GuidePage>(`/builds/${bid}/guide/${encodeURIComponent(slug)}`),
  quality: (bid: string) => apiJson<Quality>(`/builds/${bid}/quality`),
  publication: (pid: string) => apiGetWithEtag<Publication>(`/projects/${pid}/publication`),
  publish: async (pid: string, body: PublishRequest, ifMatch: string | null) =>
    json<Publication>(
      await apiSend("POST", `/projects/${pid}/publication`, body, {
        "Idempotency-Key": crypto.randomUUID(),
        ...(ifMatch ? { "If-Match": ifMatch } : {}),
      }),
    ),
  unpublish: async (pid: string, ifMatch: string) =>
    apiSend("DELETE", `/projects/${pid}/publication`, undefined, { "If-Match": ifMatch }),
  site: (slug: string) => apiJson<Site>(`/published/${encodeURIComponent(slug)}`),
  freshness: (pid: string) => apiJson<Freshness>(`/projects/${pid}/freshness`),
  audit: (wid: string, action: string | null, cursor: string | null) => {
    const params = new URLSearchParams({ limit: "50" });
    if (action) params.set("action", action);
    if (cursor) params.set("cursor", cursor);
    return apiJson<Page<AuditEntry>>(`/workspaces/${wid}/audit?${params.toString()}`);
  },
  requestExport: async (bid: string, format: ExportJob["format"]) =>
    json<ExportJob>(await apiSend("POST", `/builds/${bid}/exports`, { format }, { "Idempotency-Key": crypto.randomUUID() })),
  exportStatus: (eid: string) => apiJson<ExportJob>(`/exports/${eid}`),
  exportDownloadUrl: (eid: string) => `${API_BASE}/exports/${encodeURIComponent(eid)}/download`,
  qualityChecks: (bid: string, status: string | null, cursor: string | null) => {
    const params = new URLSearchParams({ limit: "100" });
    if (status) params.set("status", status);
    if (cursor) params.set("cursor", cursor);
    return apiJson<Page<QualityCheck>>(`/builds/${bid}/quality/checks?${params.toString()}`);
  },
  waive: async (bid: string, claimKeys: string[], reason: string) =>
    json<{ waived: string[] }>(await apiSend("POST", `/builds/${bid}/quality/waivers`, { claim_keys: claimKeys, reason })),
  search: (bid: string, q: string, kind: string | null) => {
    const params = new URLSearchParams({ q });
    if (kind) params.set("kind", kind);
    return apiJson<Page<SearchHit>>(`/builds/${bid}/search?${params.toString()}`);
  },
  fileWindow: (bid: string, fid: string, startLine: number) =>
    apiJson<FileView>(`/builds/${bid}/files/${fid}?start_line=${startLine}`),
  tree: (bid: string, path: string) =>
    apiJson<Page<TreeEntry>>(`/builds/${bid}/tree?path=${encodeURIComponent(path)}`),
  file: (bid: string, fid: string) => apiJson<FileView>(`/builds/${bid}/files/${fid}`),
  symbol: (bid: string, sid: string) => apiJson<SymbolView>(`/builds/${bid}/symbols/${sid}`),
  acceptInvitation: async (token: string) =>
    json<{ workspace_id: string }>(await apiSend("POST", "/invitations/accept", { token })),
};
