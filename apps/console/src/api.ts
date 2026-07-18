export interface UsageSummary {
  start: string;
  end: string;
  requests: number;
  successful_requests: number;
  error_requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_microusd: number;
  average_latency_ms: number | null;
}

export interface UsagePoint {
  timestamp: string;
  requests: number;
  total_tokens: number;
  cost_microusd: number;
  average_latency_ms: number | null;
}

export interface UsageSeries {
  start: string;
  end: string;
  interval: "hour" | "day";
  points: UsagePoint[];
}

export interface RouteUsage {
  route_id: string | null;
  route_name: string | null;
  provider: string;
  attempts: number;
  attempt_share_percent: number;
  successful_attempts: number;
  failed_attempts: number;
  in_flight_attempts: number;
  total_tokens: number;
  cost_microusd: number;
  average_latency_ms: number | null;
}

export interface RouteUsageReport {
  start: string;
  end: string;
  total_attempts: number;
  routes: RouteUsage[];
}

export interface TenantUsage {
  tenant_id: string | null;
  requests: number;
  successful_requests: number;
  error_requests: number;
  in_flight_requests: number;
  success_rate_percent: number;
  total_tokens: number;
  cost_microusd: number;
  average_latency_ms: number | null;
}

export interface TenantUsageReport {
  start: string;
  end: string;
  tenants: TenantUsage[];
}

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, operatorKey: string): Promise<T> {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${operatorKey}` },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(response.status, payload?.error?.message ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function loadUsage(
  operatorKey: string,
  hours: number,
  interval: "hour" | "day",
): Promise<[UsageSummary, UsageSeries, RouteUsageReport, TenantUsageReport]> {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
  const query = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
  const seriesQuery = new URLSearchParams(query);
  seriesQuery.set("interval", interval);
  return Promise.all([
    request<UsageSummary>(`/api/v1/usage/summary?${query}`, operatorKey),
    request<UsageSeries>(`/api/v1/usage/timeseries?${seriesQuery}`, operatorKey),
    request<RouteUsageReport>(`/api/v1/usage/routes?${query}`, operatorKey),
    request<TenantUsageReport>(`/api/v1/usage/tenants?${query}`, operatorKey),
  ]);
}
