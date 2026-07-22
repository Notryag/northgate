import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Input, Select, Space, Table } from "antd";
import { RotateCcw, Search } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";
import { useAuth } from "../auth";
import { loadRequests, type RequestRecord } from "../api";
import { PageHeader } from "../components/PageHeader";
import { StatusTag } from "../components/StatusTag";
import { cost, dateTime, duration, numberFormat } from "../format";

const filterSchema = z.object({
  metadataKey: z.string().min(1).max(64).regex(/^[A-Za-z0-9_.-]+$/),
  metadataValue: z.string().min(1).max(256),
});
type FilterForm = z.infer<typeof filterSchema>;

export function RequestsPage() {
  const { operatorKey } = useAuth();
  const navigate = useNavigate();
  const [hours, setHours] = useState(24);
  const [filter, setFilter] = useState<FilterForm | null>(null);
  const [requestId, setRequestId] = useState("");
  const { control, handleSubmit, reset, formState: { errors } } = useForm<FilterForm>({ resolver: zodResolver(filterSchema), defaultValues: { metadataKey: "run_id", metadataValue: "" } });
  const query = useQuery({
    queryKey: ["requests", hours, filter],
    queryFn: () => loadRequests(operatorKey, { hours, metadataKey: filter?.metadataKey, metadataValue: filter?.metadataValue, limit: 50 }),
    enabled: Boolean(operatorKey),
  });

  const columns = [
    { title: "Started", dataIndex: "started_at", width: 150, render: dateTime },
    { title: "Request", dataIndex: "request_id", width: 220, ellipsis: true, render: (value: string) => <span className="mono id-cell">{value}</span> },
    { title: "Model", dataIndex: "model", width: 120, ellipsis: true, render: (value: string | null) => value ?? "-" },
    { title: "Provider", dataIndex: "provider", width: 85, ellipsis: true, render: (value: string) => <span className="mono">{value}</span> },
    { title: "Outcome", dataIndex: "outcome", width: 140, render: (value: string) => <StatusTag value={value} /> },
    { title: "HTTP", dataIndex: "http_status", width: 60, align: "right" as const, render: (value: number | null) => value ?? "-" },
    { title: "Tokens", dataIndex: "total_tokens", width: 80, align: "right" as const, render: (value: number | null) => value == null ? "-" : numberFormat.format(value) },
    { title: "Cost", dataIndex: "cost_microusd", width: 92, align: "right" as const, render: cost },
    { title: "Latency", dataIndex: "latency_ms", width: 88, align: "right" as const, render: duration },
  ];

  return (
    <div className="page-stack">
      <PageHeader title="Requests" description="Inspect recent and correlated gateway requests without request content." actions={<Select value={hours} onChange={setHours} options={[{ value: 24, label: "Last 24 hours" }, { value: 168, label: "Last 7 days" }, { value: 720, label: "Last 30 days" }]} />} />
      <section className="filter-band">
        <form className="correlation-form" onSubmit={(event) => void handleSubmit(setFilter)(event)}>
          <label><span>Metadata key</span><Controller name="metadataKey" control={control} render={({ field }) => <Input {...field} status={errors.metadataKey ? "error" : undefined} />} /></label>
          <label className="filter-value"><span>Metadata value</span><Controller name="metadataValue" control={control} render={({ field }) => <Input {...field} status={errors.metadataValue ? "error" : undefined} placeholder="Correlation value" />} /></label>
          <Button type="primary" htmlType="submit" icon={<Search size={15} />}>Filter</Button>
          <Button icon={<RotateCcw size={15} />} onClick={() => { reset(); setFilter(null); }}>Recent</Button>
        </form>
        <Space.Compact className="request-jump">
          <Input value={requestId} onChange={(event) => setRequestId(event.target.value)} placeholder="req_..." onPressEnter={() => requestId.trim() && navigate(`/requests/${requestId.trim()}`)} />
          <Button icon={<Search size={15} />} onClick={() => requestId.trim() && navigate(`/requests/${requestId.trim()}`)}>Open</Button>
        </Space.Compact>
      </section>
      {query.error ? <Alert type="error" showIcon message="Unable to load requests" description={query.error.message} /> : null}
      <section className="work-section table-only">
        <div className="section-heading"><div><h2>{filter ? "Correlated requests" : "Recent requests"}</h2><p>{filter ? `${filter.metadataKey} · operator-selected value` : "Newest first"}{query.data?.has_more ? " · More results available" : ""}</p></div><span className="result-count">{numberFormat.format(query.data?.requests.length ?? 0)} shown</span></div>
        <Table<RequestRecord>
          size="small"
          loading={query.isLoading || query.isFetching}
          rowKey="request_id"
          dataSource={query.data?.requests ?? []}
          columns={columns}
          scroll={{ x: 1035 }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          onRow={(record) => ({ onClick: () => navigate(`/requests/${record.request_id}`) })}
          rowClassName="clickable-row"
          locale={{ emptyText: filter ? "No requests match this correlation" : "No recent requests" }}
        />
      </section>
    </div>
  );
}
