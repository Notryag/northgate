import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Descriptions, Empty, Spin, Table, Timeline } from "antd";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "../auth";
import { loadRequestDiagnostic, type Finding } from "../api";
import { PageHeader } from "../components/PageHeader";
import { StatusTag } from "../components/StatusTag";
import { cost, dateTime, duration, numberFormat } from "../format";

function findingType(severity: Finding["severity"]): "error" | "warning" | "info" {
  return severity;
}

export function RequestDetailPage() {
  const { requestId = "" } = useParams();
  const { operatorKey } = useAuth();
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ["request-diagnostic", requestId], queryFn: () => loadRequestDiagnostic(operatorKey, requestId), enabled: Boolean(operatorKey && requestId), retry: false });
  const data = query.data;
  const record = data?.request;

  return (
    <div className="page-stack">
      <PageHeader
        title="Request detail"
        description={requestId}
        actions={<><Button icon={<ArrowLeft size={15} />} onClick={() => navigate("/requests")}>Requests</Button><Button icon={<RefreshCw size={15} />} loading={query.isFetching} onClick={() => void query.refetch()}>Refresh</Button></>}
      />
      {query.error ? <Alert type="error" showIcon message="Unable to load request" description={query.error.message} /> : null}
      <Spin spinning={query.isLoading}>
        {record ? (
          <>
            <section className="detail-band">
              <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} colon={false}>
                <Descriptions.Item label="Outcome"><StatusTag value={record.outcome} /></Descriptions.Item>
                <Descriptions.Item label="HTTP status">{record.http_status ?? "-"}</Descriptions.Item>
                <Descriptions.Item label="Model">{record.model ?? "-"}</Descriptions.Item>
                <Descriptions.Item label="Provider"><span className="mono">{record.provider}</span></Descriptions.Item>
                <Descriptions.Item label="Tokens">{record.total_tokens == null ? "Unknown" : numberFormat.format(record.total_tokens)}</Descriptions.Item>
                <Descriptions.Item label="Cached prompt">{record.cached_prompt_tokens == null ? "Unknown" : numberFormat.format(record.cached_prompt_tokens)}</Descriptions.Item>
                <Descriptions.Item label="Cost">{cost(record.cost_microusd)}</Descriptions.Item>
                <Descriptions.Item label="Latency">{duration(record.latency_ms)}</Descriptions.Item>
                <Descriptions.Item label="First token">{duration(record.first_token_ms)}</Descriptions.Item>
                <Descriptions.Item label="Exact cache"><StatusTag value={record.cache_status} /></Descriptions.Item>
                <Descriptions.Item label="Started">{dateTime(record.started_at)}</Descriptions.Item>
                <Descriptions.Item label="Completed">{dateTime(record.completed_at)}</Descriptions.Item>
              </Descriptions>
            </section>
            {data.findings.length ? <section className="finding-stack" aria-label="Diagnostic findings">{data.findings.map((finding, index) => <Alert key={`${finding.code}-${index}`} type={findingType(finding.severity)} showIcon message={finding.code.replaceAll("_", " ")} description={finding.evidence ? <code>{JSON.stringify(finding.evidence)}</code> : undefined} />)}</section> : <Alert type="success" showIcon message="No diagnostic findings" />}
            <div className="detail-grid">
              <section className="work-section timeline-section">
                <div className="section-heading"><div><h2>Event timeline</h2><p>Request, provider attempts, and durable settlement</p></div></div>
                <Timeline items={[
                  { color: "blue", children: <><strong>Request accepted</strong><span>{dateTime(record.started_at)}</span></> },
                  ...data.attempts.map((attempt) => ({ color: attempt.outcome === "succeeded" ? "green" : attempt.outcome === "started" ? "orange" : "red", children: <><strong>Attempt {attempt.attempt_index} · {attempt.provider}</strong><span>{dateTime(attempt.started_at)} · {attempt.outcome} · {duration(attempt.latency_ms)}</span></> })),
                  ...data.settlement.events.map((event) => ({ color: event.status === "completed" ? "green" : event.status === "failed" ? "red" : "orange", children: <><strong>Settlement · {event.event_key}</strong><span>{dateTime(event.created_at)} · {event.status} · DB {event.database_settled_at ? "settled" : "pending"} · Policy {event.policy_settled_at ? "settled" : "pending"}</span></> })),
                ]} />
              </section>
              <section className="work-section">
                <div className="section-heading"><div><h2>Provider attempts</h2><p>{data.attempts.length} recorded</p></div></div>
                <Table
                  size="small"
                  pagination={false}
                  rowKey={(attempt) => attempt.attempt_id ?? String(attempt.attempt_index)}
                  dataSource={data.attempts}
                  scroll={{ x: 800 }}
                  columns={[
                    { title: "#", dataIndex: "attempt_index", width: 48 },
                    { title: "Provider", dataIndex: "provider", render: (value: string) => <span className="mono">{value}</span> },
                    { title: "Outcome", dataIndex: "outcome", render: (value: string) => <StatusTag value={value} /> },
                    { title: "HTTP", dataIndex: "http_status", align: "right", render: (value: number | null) => value ?? "-" },
                    { title: "Tokens", dataIndex: "total_tokens", align: "right", render: (value: number | null) => value == null ? "-" : numberFormat.format(value) },
                    { title: "Cost", dataIndex: "cost_microusd", align: "right", render: cost },
                    { title: "Latency", dataIndex: "latency_ms", align: "right", render: duration },
                  ]}
                  locale={{ emptyText: "No provider attempts recorded" }}
                />
              </section>
            </div>
          </>
        ) : !query.isLoading && !query.error ? <Empty description="Request not found" /> : null}
      </Spin>
    </div>
  );
}
