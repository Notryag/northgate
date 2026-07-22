import { useQuery } from "@tanstack/react-query";
import { Alert, Select, Table } from "antd";
import { CircleDollarSign, Clock3, Coins, Network, Users } from "lucide-react";
import { useState } from "react";
import { useAuth } from "../auth";
import { loadRouteUsage, loadTenantUsage, loadUsageSeries, loadUsageSummary } from "../api";
import { MetricStrip } from "../components/MetricStrip";
import { PageHeader } from "../components/PageHeader";
import { cost, dateTime, duration, numberFormat } from "../format";
import UsageChart from "../UsageChart";

export function UsagePage() {
  const { operatorKey } = useAuth();
  const [hours, setHours] = useState(168);
  const interval = hours <= 168 ? "hour" : "day";
  const enabled = Boolean(operatorKey);
  const summary = useQuery({ queryKey: ["usage-summary", hours], queryFn: () => loadUsageSummary(operatorKey, hours), enabled });
  const series = useQuery({ queryKey: ["usage-series", hours, interval], queryFn: () => loadUsageSeries(operatorKey, hours, interval), enabled });
  const routes = useQuery({ queryKey: ["route-usage", hours], queryFn: () => loadRouteUsage(operatorKey, hours), enabled });
  const tenants = useQuery({ queryKey: ["tenant-usage", hours], queryFn: () => loadTenantUsage(operatorKey, hours), enabled });
  const error = summary.error ?? series.error ?? routes.error ?? tenants.error;

  return (
    <div className="page-stack">
      <PageHeader title="Usage" description="Measured request, token, provider, tenant, and cost data." actions={<Select value={hours} onChange={setHours} options={[{ value: 24, label: "Last 24 hours" }, { value: 168, label: "Last 7 days" }, { value: 720, label: "Last 30 days" }]} />} />
      {error ? <Alert type="error" showIcon message="Unable to load usage" description={error.message} /> : null}
      <MetricStrip items={[
        { label: "Prompt tokens", value: summary.data ? numberFormat.format(summary.data.prompt_tokens) : "-", icon: Coins },
        { label: "Completion tokens", value: summary.data ? numberFormat.format(summary.data.completion_tokens) : "-", icon: Coins },
        { label: "Cost", value: cost(summary.data?.cost_microusd), icon: CircleDollarSign },
        { label: "Average latency", value: duration(summary.data?.average_latency_ms), icon: Clock3 },
        { label: "Provider attempts", value: routes.data ? numberFormat.format(routes.data.total_attempts) : "-", icon: Network },
      ]} />
      <section className="work-section">
        <div className="section-heading"><div><h2>Traffic and token volume</h2><p>{series.data ? `${dateTime(series.data.start)} to ${dateTime(series.data.end)}` : "-"}</p></div></div>
        <UsageChart points={series.data?.points ?? []} />
      </section>
      <section className="work-section">
        <div className="section-heading"><div><h2>Tenant usage</h2><p><Users size={13} /> Trusted tenant attribution only</p></div></div>
        <Table
          size="small"
          rowKey={(record) => record.tenant_id ?? "unattributed"}
          loading={tenants.isLoading}
          dataSource={tenants.data?.tenants ?? []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: 980 }}
          columns={[
            { title: "Tenant", dataIndex: "tenant_id", render: (value: string | null) => <span className="mono">{value ?? "Unattributed"}</span> },
            { title: "Requests", dataIndex: "requests", align: "right" },
            { title: "Success", dataIndex: "success_rate_percent", align: "right", render: (value: number) => `${value.toFixed(2)}%` },
            { title: "Failed", dataIndex: "error_requests", align: "right" },
            { title: "In flight", dataIndex: "in_flight_requests", align: "right" },
            { title: "Tokens", dataIndex: "total_tokens", align: "right", render: (value: number) => numberFormat.format(value) },
            { title: "Cost", dataIndex: "cost_microusd", align: "right", render: cost },
            { title: "Avg latency", dataIndex: "average_latency_ms", align: "right", render: duration },
          ]}
          locale={{ emptyText: "No trusted tenant usage in this range" }}
        />
      </section>
      <section className="work-section">
        <div className="section-heading"><div><h2>Route and provider usage</h2><p>Every upstream attempt, including retry and fallback</p></div></div>
        <Table
          size="small"
          rowKey={(record) => `${record.route_id ?? "configured"}-${record.provider}`}
          loading={routes.isLoading}
          dataSource={routes.data?.routes ?? []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: 1100 }}
          columns={[
            { title: "Route", dataIndex: "route_name", render: (value: string | null) => value ?? "Configured route" },
            { title: "Provider", dataIndex: "provider", render: (value: string) => <span className="mono">{value}</span> },
            { title: "Attempts", dataIndex: "attempts", align: "right" },
            { title: "Share", dataIndex: "attempt_share_percent", align: "right", render: (value: number) => `${value.toFixed(2)}%` },
            { title: "Succeeded", dataIndex: "successful_attempts", align: "right" },
            { title: "Failed", dataIndex: "failed_attempts", align: "right" },
            { title: "Tokens", dataIndex: "total_tokens", align: "right", render: (value: number) => numberFormat.format(value) },
            { title: "Cost", dataIndex: "cost_microusd", align: "right", render: cost },
            { title: "Avg latency", dataIndex: "average_latency_ms", align: "right", render: duration },
          ]}
          locale={{ emptyText: "No provider attempts in this range" }}
        />
      </section>
      <section className="work-section">
        <div className="section-heading"><div><h2>Usage buckets</h2><p>{interval === "hour" ? "Hourly" : "Daily"} aggregates</p></div></div>
        <Table
          size="small"
          rowKey="timestamp"
          dataSource={(series.data?.points ?? []).slice().reverse()}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          columns={[
            { title: "Timestamp", dataIndex: "timestamp", render: dateTime },
            { title: "Requests", dataIndex: "requests", align: "right" },
            { title: "Tokens", dataIndex: "total_tokens", align: "right", render: (value: number) => numberFormat.format(value) },
            { title: "Cost", dataIndex: "cost_microusd", align: "right", render: cost },
            { title: "Avg latency", dataIndex: "average_latency_ms", align: "right", render: duration },
          ]}
          locale={{ emptyText: "No usage buckets in this range" }}
        />
      </section>
    </div>
  );
}
