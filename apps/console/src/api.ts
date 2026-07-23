import { z } from "zod";

const nullableNumber = z.number().nullable();
const nullableString = z.string().nullable();

export const usageSummarySchema = z.object({
  start: z.string(),
  end: z.string(),
  requests: z.number(),
  successful_requests: z.number(),
  error_requests: z.number(),
  prompt_tokens: z.number(),
  completion_tokens: z.number(),
  total_tokens: z.number(),
  cost_microusd: z.number(),
  average_latency_ms: nullableNumber,
});

export const usagePointSchema = z.object({
  timestamp: z.string(),
  requests: z.number(),
  total_tokens: z.number(),
  cost_microusd: z.number(),
  average_latency_ms: nullableNumber,
});

export const usageSeriesSchema = z.object({
  start: z.string(),
  end: z.string(),
  interval: z.enum(["hour", "day"]),
  points: z.array(usagePointSchema),
});

const routeUsageSchema = z.object({
  route_id: nullableString,
  route_name: nullableString,
  provider: z.string(),
  attempts: z.number(),
  attempt_share_percent: z.number(),
  successful_attempts: z.number(),
  failed_attempts: z.number(),
  in_flight_attempts: z.number(),
  total_tokens: z.number(),
  cost_microusd: z.number(),
  average_latency_ms: nullableNumber,
});

export const routeUsageReportSchema = z.object({
  start: z.string(),
  end: z.string(),
  total_attempts: z.number(),
  routes: z.array(routeUsageSchema),
});

const tenantUsageSchema = z.object({
  tenant_id: nullableString,
  requests: z.number(),
  successful_requests: z.number(),
  error_requests: z.number(),
  in_flight_requests: z.number(),
  success_rate_percent: z.number(),
  total_tokens: z.number(),
  cost_microusd: z.number(),
  average_latency_ms: nullableNumber,
});

export const tenantUsageReportSchema = z.object({
  start: z.string(),
  end: z.string(),
  tenants: z.array(tenantUsageSchema),
});

export const modelPriceSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  provider: z.string(),
  model: z.string(),
  effective_from: z.string(),
  input_microusd_per_million: z.number(),
  output_microusd_per_million: z.number(),
});

export const requestRecordSchema = z.object({
  request_id: z.string(),
  model: nullableString,
  provider: z.string(),
  outcome: z.string(),
  http_status: nullableNumber,
  error_code: nullableString,
  estimated_tokens: z.number(),
  estimated_prompt_tokens: nullableNumber,
  reserved_output_tokens: nullableNumber,
  attempt_multiplier: nullableNumber,
  reservation_margin_tokens: nullableNumber,
  reserved_total_tokens: nullableNumber,
  actual_total_tokens: nullableNumber,
  released_tokens: nullableNumber,
  estimate_actual_ratio: nullableNumber,
  token_estimator: nullableString,
  output_limit_source: nullableString,
  prompt_tokens: nullableNumber,
  completion_tokens: nullableNumber,
  total_tokens: nullableNumber,
  cached_prompt_tokens: nullableNumber,
  cost_microusd: nullableNumber,
  cache_status: z.string(),
  metadata_trust: z.union([nullableString, z.record(z.string(), z.string())]),
  latency_ms: nullableNumber,
  first_token_ms: nullableNumber.optional(),
  started_at: z.string(),
  completed_at: nullableString,
});

export const requestsReportSchema = z.object({
  start: z.string(),
  end: z.string(),
  metadata_key: nullableString,
  metadata_value: nullableString,
  has_more: z.boolean(),
  requests: z.array(requestRecordSchema),
});

export const attemptSchema = z.object({
  attempt_id: z.string().optional(),
  attempt_index: z.number(),
  route_id: nullableString,
  provider: z.string(),
  outcome: z.string(),
  http_status: nullableNumber,
  provider_request_id: nullableString,
  prompt_tokens: nullableNumber,
  completion_tokens: nullableNumber,
  total_tokens: nullableNumber,
  cached_prompt_tokens: nullableNumber,
  cost_microusd: nullableNumber,
  latency_ms: nullableNumber,
  started_at: z.string(),
  completed_at: nullableString,
});

export const findingSchema = z.object({
  code: z.string(),
  severity: z.enum(["info", "warning", "error"]),
  request_id: z.string(),
  evidence: z.record(z.string(), z.unknown()).optional(),
});

const settlementEventSchema = z.object({
  event_id: z.string(),
  event_key: z.string(),
  schema_version: nullableNumber,
  status: z.string(),
  attempts: z.number(),
  database_settled_at: nullableString,
  policy_settled_at: nullableString,
  created_at: z.string(),
  completed_at: nullableString,
});

export const requestDiagnosticSchema = z.object({
  schema_version: z.number(),
  request: requestRecordSchema,
  attempts: z.array(attemptSchema),
  settlement: z.object({
    expected: z.boolean(),
    events: z.array(settlementEventSchema),
  }),
  findings: z.array(findingSchema),
});

const projectSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  organization_id: z.string(),
  name: z.string(),
});

const organizationSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  name: z.string(),
});

const applicationKeySchema = z.object({
  id: z.string(),
  created_at: z.string(),
  project_id: z.string(),
  name: z.string(),
  allowed_metadata_keys: z.array(z.string()),
  fixed_metadata: z.record(z.string(), z.string()),
  metadata_routing_mode: z.enum(["trusted", "legacy"]),
  revoked_at: nullableString,
});

const createdApplicationKeySchema = applicationKeySchema.omit({ revoked_at: true }).extend({
  key: z.string(),
});

const gatewaySchema = z.object({
  id: z.string(),
  created_at: z.string(),
  project_id: z.string(),
  slug: z.string(),
});

const providerCredentialSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  project_id: z.string(),
  name: z.string(),
  provider: z.string(),
  base_url: z.string(),
  adapter: z.string(),
  adapter_config: z.record(z.string(), z.string()),
});

const routeSchema = z.object({
  id: z.string(),
  created_at: z.string(),
  gateway_id: z.string(),
  provider_credential_id: z.string(),
  name: z.string(),
  priority: z.number(),
  weight: z.number(),
  match_metadata: z.record(z.string(), z.string()),
  enabled: z.boolean(),
  max_retries: z.number(),
  retry_status_codes: z.array(z.number()),
  health_failure_threshold: z.number(),
  health_recovery_seconds: z.number(),
  health_failure_status_codes: z.array(z.number()),
  default_max_output_tokens: nullableNumber,
});

const policySchema = z.object({
  id: z.string(),
  created_at: z.string(),
  gateway_id: z.string(),
  requests_per_minute: nullableNumber,
  concurrent_requests: nullableNumber,
  tokens_per_day: nullableNumber,
  daily_spend_microusd: nullableNumber,
  monthly_spend_microusd: nullableNumber,
  exact_cache_ttl_seconds: nullableNumber,
});

const concurrencyLeaseSchema = z.object({
  policy_key: z.string(),
  started_at: nullableString,
  expires_at: z.string(),
  age_seconds: nullableNumber,
  expired: z.boolean(),
});

const staleRequestDiagnosticSchema = requestDiagnosticSchema.extend({
  stale: z.object({
    request_started: z.boolean(),
    stale_attempt_indexes: z.array(z.number()),
    age_seconds: z.number(),
    recoverable_settlement: z.boolean(),
    concurrency_leases: z.array(concurrencyLeaseSchema),
  }),
});

const staleDiagnosticsSchema = z.object({
  schema_version: z.number(),
  cutoff: z.string(),
  minimum_age_seconds: z.number(),
  has_more: z.boolean(),
  policy_state_available: z.boolean(),
  policy_keys_truncated: z.boolean(),
  finding_counts: z.record(z.string(), z.number()),
  requests: z.array(staleRequestDiagnosticSchema),
  findings: z.array(findingSchema),
});

const readinessSchema = z.object({
  status: z.enum(["ready", "not_ready"]),
  degraded: z.boolean().optional(),
  reason: z.string().optional(),
  settlement_backlog: z.object({
    pending_events: z.number(),
    oldest_age_seconds: z.number(),
  }).optional(),
});

export type UsageSummary = z.infer<typeof usageSummarySchema>;
export type UsagePoint = z.infer<typeof usagePointSchema>;
export type UsageSeries = z.infer<typeof usageSeriesSchema>;
export type RouteUsageReport = z.infer<typeof routeUsageReportSchema>;
export type TenantUsageReport = z.infer<typeof tenantUsageReportSchema>;
export type ModelPrice = z.infer<typeof modelPriceSchema>;
export type RequestRecord = z.infer<typeof requestRecordSchema>;
export type RequestsReport = z.infer<typeof requestsReportSchema>;
export type Attempt = z.infer<typeof attemptSchema>;
export type Finding = z.infer<typeof findingSchema>;
export type RequestDiagnostic = z.infer<typeof requestDiagnosticSchema>;
export type Organization = z.infer<typeof organizationSchema>;
export type Project = z.infer<typeof projectSchema>;
export type ApplicationKey = z.infer<typeof applicationKeySchema>;
export type CreatedApplicationKey = z.infer<typeof createdApplicationKeySchema>;
export type Gateway = z.infer<typeof gatewaySchema>;
export type ProviderCredential = z.infer<typeof providerCredentialSchema>;
export type Route = z.infer<typeof routeSchema>;
export type GatewayPolicy = z.infer<typeof policySchema>;
export type StaleDiagnostics = z.infer<typeof staleDiagnosticsSchema>;
export type Readiness = z.infer<typeof readinessSchema>;

export interface ApplicationKeyCreateInput {
  project_id: string;
  name: string;
  allowed_metadata_keys: string[];
  fixed_metadata: Record<string, string>;
  metadata_routing_mode: "trusted" | "legacy";
}

export interface ProviderCredentialCreateInput {
  project_id: string;
  name: string;
  provider: string;
  base_url: string;
  adapter: "openai_compatible" | "azure_openai";
  adapter_config: Record<string, string>;
  api_key: string;
}

export interface ModelPriceCreateInput {
  provider: string;
  model: string;
  effective_from: string;
  input_microusd_per_million: number;
  output_microusd_per_million: number;
}

export interface RouteCreateInput {
  gateway_id: string;
  provider_credential_id: string;
  name: string;
  priority: number;
  weight: number;
  match_metadata: Record<string, string>;
  enabled: boolean;
  max_retries: number;
  retry_status_codes: number[];
  health_failure_threshold: number;
  health_recovery_seconds: number;
  health_failure_status_codes: number[];
  default_max_output_tokens: number | null;
}

export interface PolicyInput {
  requests_per_minute: number | null;
  concurrent_requests: number | null;
  tokens_per_day: number | null;
  daily_spend_microusd: number | null;
  monthly_spend_microusd: number | null;
  exact_cache_ttl_seconds: number | null;
}

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

async function request<T>(
  path: string,
  operatorKey: string,
  schema: z.ZodType<T>,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${operatorKey}`);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    if (response.status === 401) window.dispatchEvent(new Event("northgate:unauthorized"));
    throw new ApiError(response.status, payload?.error?.message ?? `Request failed (${response.status})`);
  }
  return schema.parse(await response.json());
}

async function requestEmpty(path: string, operatorKey: string, init: RequestInit): Promise<void> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${operatorKey}`);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    if (response.status === 401) window.dispatchEvent(new Event("northgate:unauthorized"));
    throw new ApiError(response.status, payload?.error?.message ?? `Request failed (${response.status})`);
  }
}

function rangeQuery(hours: number): URLSearchParams {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
  return new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
}

export async function loadUsageSummary(operatorKey: string, hours: number) {
  return request(`/api/v1/usage/summary?${rangeQuery(hours)}`, operatorKey, usageSummarySchema);
}

export async function loadUsageSeries(
  operatorKey: string,
  hours: number,
  interval: "hour" | "day",
) {
  const query = rangeQuery(hours);
  query.set("interval", interval);
  return request(`/api/v1/usage/timeseries?${query}`, operatorKey, usageSeriesSchema);
}

export async function loadRouteUsage(operatorKey: string, hours: number) {
  return request(`/api/v1/usage/routes?${rangeQuery(hours)}`, operatorKey, routeUsageReportSchema);
}

export async function loadTenantUsage(operatorKey: string, hours: number) {
  return request(`/api/v1/usage/tenants?${rangeQuery(hours)}`, operatorKey, tenantUsageReportSchema);
}

export async function loadRequests(
  operatorKey: string,
  options: { hours: number; metadataKey?: string; metadataValue?: string; limit?: number },
) {
  const query = rangeQuery(options.hours);
  query.set("limit", String(options.limit ?? 50));
  if (options.metadataKey && options.metadataValue) {
    query.set("metadata_key", options.metadataKey);
    query.set("metadata_value", options.metadataValue);
  }
  return request(`/api/v1/usage/requests?${query}`, operatorKey, requestsReportSchema);
}

export async function loadRequestDiagnostic(operatorKey: string, requestId: string) {
  return request(
    `/api/v1/diagnostics/requests/${encodeURIComponent(requestId)}`,
    operatorKey,
    requestDiagnosticSchema,
  );
}

export async function loadModelPrices(operatorKey: string) {
  return request("/api/v1/model-prices", operatorKey, z.array(modelPriceSchema));
}

export async function createModelPrice(operatorKey: string, input: ModelPriceCreateInput) {
  return request("/api/v1/model-prices", operatorKey, modelPriceSchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function loadProjects(operatorKey: string) {
  return request("/api/v1/projects", operatorKey, z.array(projectSchema));
}

export async function loadOrganizations(operatorKey: string) {
  return request("/api/v1/organizations", operatorKey, z.array(organizationSchema));
}

export async function createOrganization(operatorKey: string, input: { name: string }) {
  return request("/api/v1/organizations", operatorKey, organizationSchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function createProject(
  operatorKey: string,
  input: { organization_id: string; name: string },
) {
  return request("/api/v1/projects", operatorKey, projectSchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function loadApplicationKeys(operatorKey: string) {
  return request("/api/v1/application-keys", operatorKey, z.array(applicationKeySchema));
}

export async function createApplicationKey(
  operatorKey: string,
  input: ApplicationKeyCreateInput,
) {
  return request("/api/v1/application-keys", operatorKey, createdApplicationKeySchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function revokeApplicationKey(operatorKey: string, keyId: string) {
  return request(
    `/api/v1/application-keys/${encodeURIComponent(keyId)}/revoke`,
    operatorKey,
    applicationKeySchema.pick({ id: true, created_at: true, revoked_at: true }),
    { method: "POST" },
  );
}

export async function loadGateways(operatorKey: string) {
  return request("/api/v1/gateways", operatorKey, z.array(gatewaySchema));
}

export async function createGateway(operatorKey: string, input: { project_id: string; slug: string }) {
  return request("/api/v1/gateways", operatorKey, gatewaySchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function loadProviderCredentials(operatorKey: string) {
  return request("/api/v1/provider-credentials", operatorKey, z.array(providerCredentialSchema));
}

export async function createProviderCredential(
  operatorKey: string,
  input: ProviderCredentialCreateInput,
) {
  return request("/api/v1/provider-credentials", operatorKey, providerCredentialSchema, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function rotateProviderCredential(
  operatorKey: string,
  credentialId: string,
  apiKey: string,
) {
  return requestEmpty(
    `/api/v1/provider-credentials/${encodeURIComponent(credentialId)}/secret`,
    operatorKey,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    },
  );
}

export async function loadRoutes(operatorKey: string) {
  return request("/api/v1/routes", operatorKey, z.array(routeSchema));
}

export async function createRoute(operatorKey: string, input: RouteCreateInput) {
  await request("/api/v1/routes", operatorKey, routeSchema.pick({
    id: true,
    created_at: true,
    gateway_id: true,
    provider_credential_id: true,
    name: true,
    priority: true,
    weight: true,
    match_metadata: true,
    enabled: true,
    default_max_output_tokens: true,
  }), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function updateRoute(
  operatorKey: string,
  routeId: string,
  input: { priority?: number; weight?: number; enabled?: boolean; default_max_output_tokens?: number | null },
) {
  return request(`/api/v1/routes/${encodeURIComponent(routeId)}`, operatorKey, routeSchema.pick({
    id: true,
    created_at: true,
    priority: true,
    weight: true,
    enabled: true,
    default_max_output_tokens: true,
  }), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function loadPolicies(operatorKey: string) {
  return request("/api/v1/policies", operatorKey, z.array(policySchema));
}

export async function replacePolicy(operatorKey: string, gatewayId: string, input: PolicyInput) {
  return request(`/api/v1/policies/${encodeURIComponent(gatewayId)}`, operatorKey, policySchema, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export async function loadReadiness(): Promise<Readiness> {
  const response = await fetch("/health/ready");
  return readinessSchema.parse(await response.json());
}

export async function loadStaleDiagnostics(
  operatorKey: string,
  minimumAgeSeconds: number,
  limit = 100,
) {
  const query = new URLSearchParams({
    minimum_age_seconds: String(minimumAgeSeconds),
    limit: String(limit),
  });
  return request(`/api/v1/diagnostics/stale?${query}`, operatorKey, staleDiagnosticsSchema);
}
