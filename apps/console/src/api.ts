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
): Promise<[UsageSummary, UsageSeries]> {
  const end = new Date();
  const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
  const query = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
  const seriesQuery = new URLSearchParams(query);
  seriesQuery.set("interval", interval);
  return Promise.all([
    request<UsageSummary>(`/api/v1/usage/summary?${query}`, operatorKey),
    request<UsageSeries>(`/api/v1/usage/timeseries?${seriesQuery}`, operatorKey),
  ]);
}
