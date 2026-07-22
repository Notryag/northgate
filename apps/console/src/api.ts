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

export interface ModelPriceCreateInput {
  provider: string;
  model: string;
  effective_from: string;
  input_microusd_per_million: number;
  output_microusd_per_million: number;
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
