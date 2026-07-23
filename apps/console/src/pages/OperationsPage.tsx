import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Select, Table, Tag } from "antd";
import { Activity, Clock3, DatabaseZap, RefreshCw, ShieldAlert } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { loadReadiness, loadStaleDiagnostics } from "../api";
import { MetricStrip } from "../components/MetricStrip";
import { PageHeader } from "../components/PageHeader";
import { StatusTag } from "../components/StatusTag";
import { numberFormat } from "../format";

const ageOptions = [
  { value: 300, label: "5 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 3600, label: "1 hour" },
  { value: 21600, label: "6 hours" },
];

export function OperationsPage() {
  const { operatorKey } = useAuth();
  const navigate = useNavigate();
  const [minimumAgeSeconds, setMinimumAgeSeconds] = useState(900);
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: loadReadiness, refetchInterval: 15_000 });
  const stale = useQuery({ queryKey: ["stale-diagnostics", minimumAgeSeconds], queryFn: () => loadStaleDiagnostics(operatorKey, minimumAgeSeconds), enabled: Boolean(operatorKey) });

  const summary = useMemo(() => {
    const requests = stale.data?.requests ?? [];
    const leases = requests.flatMap((item) => item.stale.concurrency_leases);
    return {
      staleRequests: requests.length,
      recoverable: requests.filter((item) => item.stale.recoverable_settlement).length,
      unprotected: requests.filter((item) => !item.stale.recoverable_settlement).length,
      activeLeases: leases.filter((item) => !item.expired).length,
      expiredLeases: leases.filter((item) => item.expired).length,
    };
  }, [stale.data]);

  const error = readiness.error ?? stale.error;
  const readinessValue = readiness.data?.degraded ? "degraded" : readiness.data?.status ?? "unknown";

  return (
    <div className="page-stack">
      <PageHeader title="Operations" description="Inspect readiness, settlement recovery state, stale ledgers, and concurrency leases." actions={<Button icon={<RefreshCw size={15} />} loading={readiness.isFetching || stale.isFetching} onClick={() => { void readiness.refetch(); void stale.refetch(); }}>Refresh</Button>} />
      {error ? <Alert type="error" showIcon message="Operational query failed" description={error.message} /> : null}
      {readiness.data?.degraded ? <Alert type="warning" showIcon message="Data plane is ready but degraded" description={readiness.data.reason?.replaceAll("_", " ")} /> : null}
      {readiness.data?.status === "not_ready" ? <Alert type="error" showIcon message="Data plane is not ready" description={readiness.data.reason?.replaceAll("_", " ") ?? "A required dependency is unavailable"} /> : null}
      <MetricStrip items={[
        { label: "Readiness", value: readinessValue.replaceAll("_", " "), icon: Activity, tone: readinessValue === "ready" ? "healthy" : readinessValue === "degraded" ? "warning" : "error" },
        { label: "Pending events", value: numberFormat.format(readiness.data?.settlement_backlog?.pending_events ?? 0), icon: DatabaseZap, tone: (readiness.data?.settlement_backlog?.pending_events ?? 0) > 0 ? "warning" : "healthy" },
        { label: "Stale requests", value: numberFormat.format(summary.staleRequests), icon: Clock3, tone: summary.staleRequests > 0 ? "warning" : "healthy" },
        { label: "Unprotected", value: numberFormat.format(summary.unprotected), icon: ShieldAlert, tone: summary.unprotected > 0 ? "error" : "healthy" },
        { label: "Expired leases", value: numberFormat.format(summary.expiredLeases), icon: ShieldAlert, tone: summary.expiredLeases > 0 ? "error" : "healthy" },
      ]} />
      <section className="filter-band operations-filter">
        <label><span>Minimum stale age</span><Select value={minimumAgeSeconds} onChange={setMinimumAgeSeconds} options={ageOptions} /></label>
        <div className="operations-status"><span>Policy state</span><StatusTag value={stale.data?.policy_state_available ? "ready" : "unavailable"} />{stale.data?.policy_keys_truncated ? <Tag color="warning">Policy scan truncated</Tag> : null}{stale.data?.has_more ? <Tag color="warning">Results truncated</Tag> : null}</div>
      </section>
      <section className="work-section table-only">
        <div className="section-heading"><div><h2>Stale settlement records</h2><p>Request, attempt, outbox, and concurrency lease state joined by request ID.</p></div><span className="result-count">{numberFormat.format(stale.data?.requests.length ?? 0)} records</span></div>
        <Table
          size="small"
          rowKey={(record) => record.request.request_id}
          pagination={false}
          loading={stale.isLoading}
          dataSource={stale.data?.requests ?? []}
          scroll={{ x: 1120 }}
          onRow={(record) => ({ className: "clickable-row", onClick: () => navigate(`/requests/${encodeURIComponent(record.request.request_id)}`) })}
          columns={[
            { title: "Request ID", dataIndex: ["request", "request_id"], width: 285, render: (value: string) => <code className="mono id-cell">{value}</code> },
            { title: "Age", dataIndex: ["stale", "age_seconds"], width: 100, render: (value: number) => `${numberFormat.format(value)}s` },
            { title: "Outcome", dataIndex: ["request", "outcome"], width: 130, render: (value: string) => <StatusTag value={value} /> },
            { title: "Attempts", dataIndex: "attempts", width: 95, align: "right" as const, render: (value: unknown[]) => numberFormat.format(value.length) },
            { title: "Stale attempts", dataIndex: ["stale", "stale_attempt_indexes"], width: 125, render: (value: number[]) => value.length ? value.join(", ") : <span className="muted">None</span> },
            { title: "Recovery", dataIndex: ["stale", "recoverable_settlement"], width: 130, render: (value: boolean) => <Tag color={value ? "green" : "red"}>{value ? "Outbox protected" : "Unprotected"}</Tag> },
            { title: "Events", dataIndex: ["settlement", "events"], width: 150, render: (events: { status: string }[]) => events.length ? <div className="tag-list">{events.map((event, index) => <StatusTag key={`${event.status}-${index}`} value={event.status} />)}</div> : <span className="muted">None</span> },
            { title: "Leases", dataIndex: ["stale", "concurrency_leases"], width: 105, render: (leases: { expired: boolean }[]) => leases.length ? <Tag color={leases.some((item) => item.expired) ? "red" : "gold"}>{leases.length} {leases.some((item) => item.expired) ? "expired" : "active"}</Tag> : <span className="muted">None</span> },
            { title: "Findings", dataIndex: "findings", render: (findings: { code: string; severity: string }[]) => findings.length ? <div className="tag-list">{findings.map((finding, index) => <Tag key={`${finding.code}-${index}`} color={finding.severity === "error" ? "red" : finding.severity === "warning" ? "gold" : undefined}>{finding.code}</Tag>)}</div> : <span className="muted">None</span> },
          ]}
          locale={{ emptyText: "No stale settlement records in this age window" }}
        />
      </section>
      <section className="detail-band operations-detail">
        <div><span>Recoverable records</span><strong>{numberFormat.format(summary.recoverable)}</strong></div>
        <div><span>Active leases</span><strong>{numberFormat.format(summary.activeLeases)}</strong></div>
        <div><span>Oldest pending event</span><strong>{readiness.data?.settlement_backlog ? `${numberFormat.format(Math.round(readiness.data.settlement_backlog.oldest_age_seconds))}s` : "None"}</strong></div>
        <div><span>Diagnostic cutoff</span><strong>{stale.data?.cutoff ? new Date(stale.data.cutoff).toLocaleString() : "Unknown"}</strong></div>
      </section>
    </div>
  );
}
