import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Select, Spin, Table } from "antd";
import { Activity, CircleDollarSign, Clock3, Coins, RefreshCw, ShieldCheck } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useState } from "react";
import { useAuth } from "../auth";
import { loadRequests, loadRouteUsage, loadUsageSeries, loadUsageSummary } from "../api";
import { MetricStrip } from "../components/MetricStrip";
import { PageHeader } from "../components/PageHeader";
import { StatusTag } from "../components/StatusTag";
import { cost, dateTime, duration, numberFormat } from "../format";
import UsageChart from "../UsageChart";

export function OverviewPage() {
  const { operatorKey } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [hours, setHours] = useState(24);
  const enabled = Boolean(operatorKey);
  const summary = useQuery({ queryKey: ["usage-summary", hours], queryFn: () => loadUsageSummary(operatorKey, hours), enabled });
  const series = useQuery({ queryKey: ["usage-series", hours, "hour"], queryFn: () => loadUsageSeries(operatorKey, hours, hours > 168 ? "day" : "hour"), enabled });
  const routes = useQuery({ queryKey: ["route-usage", hours], queryFn: () => loadRouteUsage(operatorKey, hours), enabled });
  const recent = useQuery({ queryKey: ["requests", hours, "recent", 8], queryFn: () => loadRequests(operatorKey, { hours, limit: 8 }), enabled });
  const data = summary.data;
  const errorRate = data?.requests ? data.error_requests * 100 / data.requests : 0;
  const loading = summary.isLoading || series.isLoading || routes.isLoading || recent.isLoading;
  const error = summary.error ?? series.error ?? routes.error ?? recent.error;

  return (
    <div className="page-stack">
      <PageHeader
        title="Overview"
        description="Current traffic, spend, latency, and request outcomes."
        actions={<><Select value={hours} onChange={setHours} options={[{ value: 24, label: "Last 24 hours" }, { value: 168, label: "Last 7 days" }, { value: 720, label: "Last 30 days" }]} /><Button icon={<RefreshCw size={15} />} loading={loading} onClick={() => void queryClient.invalidateQueries()}>Refresh</Button></>}
      />
      {error ? <Alert type="error" showIcon message="Unable to load overview" description={error.message} /> : null}
      <Spin spinning={loading && !data}>
        <MetricStrip items={[
          { label: "Requests", value: data ? numberFormat.format(data.requests) : "-", icon: Activity },
          { label: "Total tokens", value: data ? numberFormat.format(data.total_tokens) : "-", icon: Coins },
          { label: "Cost", value: cost(data?.cost_microusd), icon: CircleDollarSign },
          { label: "Average latency", value: duration(data?.average_latency_ms), icon: Clock3 },
          { label: "Error rate", value: `${errorRate.toFixed(2)}%`, icon: ShieldCheck, tone: errorRate > 0 ? "error" : "healthy" },
        ]} />
      </Spin>
      <section className="work-section">
        <div className="section-heading"><div><h2>Traffic and token volume</h2><p>{series.data ? `${dateTime(series.data.start)} to ${dateTime(series.data.end)}` : "-"}</p></div><div className="chart-legend"><span className="requests-key">Requests</span><span className="tokens-key">Tokens</span></div></div>
        <UsageChart points={series.data?.points ?? []} />
      </section>
      <div className="overview-grid">
        <section className="work-section">
          <div className="section-heading"><div><h2>Recent requests</h2><p>Newest requests in the selected range</p></div><Button type="link" onClick={() => navigate("/requests")}>View all</Button></div>
          <Table
            size="small"
            rowKey="request_id"
            pagination={false}
            dataSource={recent.data?.requests ?? []}
            onRow={(record) => ({ onClick: () => navigate(`/requests/${record.request_id}`) })}
            rowClassName="clickable-row"
            columns={[
              { title: "Request", dataIndex: "request_id", ellipsis: true, render: (value: string) => <span className="mono id-cell">{value}</span> },
              { title: "Outcome", dataIndex: "outcome", width: 150, render: (value: string) => <StatusTag value={value} /> },
              { title: "Latency", dataIndex: "latency_ms", width: 100, align: "right", render: duration },
            ]}
            locale={{ emptyText: "No requests in this range" }}
          />
        </section>
        <section className="work-section">
          <div className="section-heading"><div><h2>Provider traffic</h2><p>{numberFormat.format(routes.data?.total_attempts ?? 0)} upstream attempts</p></div></div>
          <Table
            size="small"
            rowKey={(record) => `${record.route_id ?? "configured"}-${record.provider}`}
            pagination={false}
            dataSource={(routes.data?.routes ?? []).slice(0, 8)}
            columns={[
              { title: "Route", dataIndex: "route_name", render: (value: string | null) => value ?? "Configured route" },
              { title: "Provider", dataIndex: "provider", render: (value: string) => <span className="mono">{value}</span> },
              { title: "Attempts", dataIndex: "attempts", align: "right", width: 90 },
              { title: "Failed", dataIndex: "failed_attempts", align: "right", width: 80 },
            ]}
            locale={{ emptyText: "No provider attempts in this range" }}
          />
        </section>
      </div>
    </div>
  );
}
