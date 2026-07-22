import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, InputNumber, Modal, Select, Switch, Table, Tag } from "antd";
import { Pencil, Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Controller, useFieldArray, useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../auth";
import {
  createGateway,
  createRoute,
  loadGateways,
  loadPolicies,
  loadProjects,
  loadProviderCredentials,
  loadRoutes,
  replacePolicy,
  updateRoute,
  type Route,
} from "../api";
import { MetricStrip } from "../components/MetricStrip";
import { PageHeader } from "../components/PageHeader";
import { StatusTag } from "../components/StatusTag";
import { cost, numberFormat } from "../format";
import { Gauge, Network, Route as RouteIcon, ShieldCheck } from "lucide-react";

const gatewaySchema = z.object({
  projectId: z.string().min(1),
  slug: z.string().min(1).max(120).regex(/^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$/),
});
type GatewayForm = z.infer<typeof gatewaySchema>;

const routeFormSchema = z.object({
  credentialId: z.string().min(1),
  name: z.string().trim().min(1).max(200),
  priority: z.number().int().min(0).max(10000),
  weight: z.number().int().min(1).max(10000),
  maxRetries: z.number().int().min(0).max(5),
  healthFailureThreshold: z.number().int().min(0).max(100),
  healthRecoverySeconds: z.number().int().min(1).max(3600),
  enabled: z.boolean(),
  metadata: z.array(z.object({ key: z.string().max(64), value: z.string().max(256) })).max(16),
}).superRefine((value, context) => {
  const keys = new Set<string>();
  value.metadata.forEach((item, index) => {
    if (!item.key && !item.value) return;
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(item.key) || !item.value) {
      context.addIssue({ code: "custom", message: "Complete both metadata fields", path: ["metadata", index] });
    } else if (keys.has(item.key)) {
      context.addIssue({ code: "custom", message: "Metadata keys must be unique", path: ["metadata", index, "key"] });
    }
    keys.add(item.key);
  });
});
type RouteForm = z.infer<typeof routeFormSchema>;

const editRouteSchema = z.object({
  priority: z.number().int().min(0).max(10000),
  weight: z.number().int().min(1).max(10000),
  enabled: z.boolean(),
});
type EditRouteForm = z.infer<typeof editRouteSchema>;

const nullableLimit = z.number().int().positive().nullable();
const policyFormSchema = z.object({
  requestsPerMinute: nullableLimit,
  concurrentRequests: nullableLimit,
  tokensPerDay: nullableLimit,
  dailySpendUsd: z.number().positive().nullable(),
  monthlySpendUsd: z.number().positive().nullable(),
  exactCacheTtlSeconds: nullableLimit,
});
type PolicyForm = z.infer<typeof policyFormSchema>;

const routeDefaults: RouteForm = {
  credentialId: "",
  name: "",
  priority: 0,
  weight: 1,
  maxRetries: 0,
  healthFailureThreshold: 0,
  healthRecoverySeconds: 30,
  enabled: true,
  metadata: [{ key: "", value: "" }],
};

export function GatewaysPage() {
  const { operatorKey } = useAuth();
  const queryClient = useQueryClient();
  const enabled = Boolean(operatorKey);
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => loadProjects(operatorKey), enabled });
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => loadGateways(operatorKey), enabled });
  const credentials = useQuery({ queryKey: ["provider-credentials"], queryFn: () => loadProviderCredentials(operatorKey), enabled });
  const routes = useQuery({ queryKey: ["routes"], queryFn: () => loadRoutes(operatorKey), enabled });
  const policies = useQuery({ queryKey: ["policies"], queryFn: () => loadPolicies(operatorKey), enabled });
  const [selectedGatewayId, setSelectedGatewayId] = useState("");
  const [gatewayOpen, setGatewayOpen] = useState(false);
  const [routeOpen, setRouteOpen] = useState(false);
  const [editingRoute, setEditingRoute] = useState<Route | null>(null);

  useEffect(() => {
    if (!selectedGatewayId && gateways.data?.[0]) setSelectedGatewayId(gateways.data[0].id);
  }, [gateways.data, selectedGatewayId]);

  const selectedGateway = gateways.data?.find((item) => item.id === selectedGatewayId);
  const selectedProject = projects.data?.find((item) => item.id === selectedGateway?.project_id);
  const gatewayRoutes = (routes.data ?? []).filter((item) => item.gateway_id === selectedGatewayId);
  const gatewayPolicy = policies.data?.find((item) => item.gateway_id === selectedGatewayId);
  const projectCredentials = (credentials.data ?? []).filter((item) => item.project_id === selectedGateway?.project_id);
  const credentialById = useMemo(() => new Map((credentials.data ?? []).map((item) => [item.id, item])), [credentials.data]);
  const error = projects.error ?? gateways.error ?? credentials.error ?? routes.error ?? policies.error;

  const gatewayForm = useForm<GatewayForm>({ resolver: zodResolver(gatewaySchema), defaultValues: { projectId: "", slug: "" } });
  const routeForm = useForm<RouteForm>({ resolver: zodResolver(routeFormSchema), defaultValues: routeDefaults });
  const metadataFields = useFieldArray({ control: routeForm.control, name: "metadata" });
  const editForm = useForm<EditRouteForm>({ resolver: zodResolver(editRouteSchema), defaultValues: { priority: 0, weight: 1, enabled: true } });
  const policyForm = useForm<PolicyForm>({ resolver: zodResolver(policyFormSchema), defaultValues: { requestsPerMinute: null, concurrentRequests: null, tokensPerDay: null, dailySpendUsd: null, monthlySpendUsd: null, exactCacheTtlSeconds: null } });

  useEffect(() => {
    policyForm.reset({
      requestsPerMinute: gatewayPolicy?.requests_per_minute ?? null,
      concurrentRequests: gatewayPolicy?.concurrent_requests ?? null,
      tokensPerDay: gatewayPolicy?.tokens_per_day ?? null,
      dailySpendUsd: gatewayPolicy?.daily_spend_microusd == null ? null : gatewayPolicy.daily_spend_microusd / 1_000_000,
      monthlySpendUsd: gatewayPolicy?.monthly_spend_microusd == null ? null : gatewayPolicy.monthly_spend_microusd / 1_000_000,
      exactCacheTtlSeconds: gatewayPolicy?.exact_cache_ttl_seconds ?? null,
    });
  }, [gatewayPolicy, policyForm]);

  const createGatewayMutation = useMutation({
    mutationFn: (values: GatewayForm) => createGateway(operatorKey, { project_id: values.projectId, slug: values.slug }),
    onSuccess: async (created) => { await queryClient.invalidateQueries({ queryKey: ["gateways"] }); setSelectedGatewayId(created.id); setGatewayOpen(false); gatewayForm.reset(); },
  });
  const createRouteMutation = useMutation({
    mutationFn: (values: RouteForm) => createRoute(operatorKey, {
      gateway_id: selectedGatewayId,
      provider_credential_id: values.credentialId,
      name: values.name.trim(),
      priority: values.priority,
      weight: values.weight,
      enabled: values.enabled,
      max_retries: values.maxRetries,
      retry_status_codes: [429, 500, 502, 503, 504],
      health_failure_threshold: values.healthFailureThreshold,
      health_recovery_seconds: values.healthRecoverySeconds,
      health_failure_status_codes: [500, 502, 503, 504],
      match_metadata: Object.fromEntries(values.metadata.filter((item) => item.key && item.value).map((item) => [item.key, item.value])),
    }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["routes"] }); setRouteOpen(false); routeForm.reset(routeDefaults); },
  });
  const editRouteMutation = useMutation({
    mutationFn: (values: EditRouteForm) => updateRoute(operatorKey, editingRoute!.id, values),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["routes"] }); setEditingRoute(null); },
  });
  const policyMutation = useMutation({
    mutationFn: (values: PolicyForm) => replacePolicy(operatorKey, selectedGatewayId, {
      requests_per_minute: values.requestsPerMinute,
      concurrent_requests: values.concurrentRequests,
      tokens_per_day: values.tokensPerDay,
      daily_spend_microusd: values.dailySpendUsd == null ? null : Math.round(values.dailySpendUsd * 1_000_000),
      monthly_spend_microusd: values.monthlySpendUsd == null ? null : Math.round(values.monthlySpendUsd * 1_000_000),
      exact_cache_ttl_seconds: values.exactCacheTtlSeconds,
    }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["policies"] }); },
  });

  const mutationError = createGatewayMutation.error ?? createRouteMutation.error ?? editRouteMutation.error ?? policyMutation.error;

  return (
    <div className="page-stack">
      <PageHeader title="Gateways" description="Organize routes, traffic selection, health behavior, and gateway policy." actions={<Button type="primary" icon={<Plus size={15} />} onClick={() => { gatewayForm.reset({ projectId: projects.data?.[0]?.id ?? "", slug: "" }); setGatewayOpen(true); }}>New gateway</Button>} />
      {error || mutationError ? <Alert type="error" showIcon message="Gateway operation failed" description={(error ?? mutationError)?.message} /> : null}
      <section className="gateway-selector">
        <label><span>Gateway</span><Select value={selectedGatewayId || undefined} loading={gateways.isLoading} onChange={setSelectedGatewayId} options={(gateways.data ?? []).map((item) => ({ value: item.id, label: `${projects.data?.find((project) => project.id === item.project_id)?.name ?? "Project"} / ${item.slug}` }))} placeholder="Select a gateway" /></label>
        {selectedGateway ? <div className="gateway-identity"><span>Project</span><strong>{selectedProject?.name ?? selectedGateway.project_id}</strong><code>{selectedGateway.id}</code></div> : null}
      </section>
      {selectedGateway ? <>
        <MetricStrip items={[
          { label: "Routes", value: numberFormat.format(gatewayRoutes.length), icon: RouteIcon },
          { label: "Enabled routes", value: numberFormat.format(gatewayRoutes.filter((route) => route.enabled).length), icon: Network, tone: gatewayRoutes.some((route) => route.enabled) ? "healthy" : "warning" },
          { label: "Credentials", value: numberFormat.format(projectCredentials.length), icon: ShieldCheck },
          { label: "Request limit", value: gatewayPolicy?.requests_per_minute == null ? "Unlimited" : `${numberFormat.format(gatewayPolicy.requests_per_minute)} / min`, icon: Gauge },
          { label: "Monthly budget", value: gatewayPolicy?.monthly_spend_microusd == null ? "Unlimited" : cost(gatewayPolicy.monthly_spend_microusd), icon: ShieldCheck },
        ]} />
        <section className="work-section table-only">
          <div className="section-heading"><div><h2>Routes</h2><p>Ordered provider candidates for this gateway</p></div><Button icon={<Plus size={15} />} disabled={!projectCredentials.length} onClick={() => { routeForm.reset({ ...routeDefaults, credentialId: projectCredentials[0]?.id ?? "" }); setRouteOpen(true); }}>Add route</Button></div>
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={gatewayRoutes}
            scroll={{ x: 980 }}
            columns={[
              { title: "Route", dataIndex: "name", render: (value: string) => <strong>{value}</strong> },
              { title: "Provider", dataIndex: "provider_credential_id", render: (value: string) => { const credential = credentialById.get(value); return <span><span className="mono">{credential?.provider ?? "unknown"}</span><small className="cell-subtitle">{credential?.name ?? value}</small></span>; } },
              { title: "Match", dataIndex: "match_metadata", render: (value: Record<string, string>) => Object.keys(value).length ? Object.entries(value).map(([key, item]) => <Tag key={key}>{key}={item}</Tag>) : <span className="muted">Default</span> },
              { title: "Priority", dataIndex: "priority", align: "right", width: 80 },
              { title: "Weight", dataIndex: "weight", align: "right", width: 80 },
              { title: "Retries", dataIndex: "max_retries", align: "right", width: 75 },
              { title: "Circuit", dataIndex: "health_failure_threshold", width: 110, render: (value: number, record: Route) => value ? `${value} / ${record.health_recovery_seconds}s` : "Disabled" },
              { title: "Status", dataIndex: "enabled", width: 95, render: (value: boolean) => <StatusTag value={value ? "ready" : "disabled"} /> },
              { title: "", width: 48, render: (_: unknown, record: Route) => <Button type="text" icon={<Pencil size={14} />} aria-label={`Edit ${record.name}`} onClick={() => { editForm.reset({ priority: record.priority, weight: record.weight, enabled: record.enabled }); setEditingRoute(record); }} /> },
            ]}
            locale={{ emptyText: "No routes configured" }}
          />
        </section>
        <section className="form-band">
          <div className="section-heading"><div><h2>Gateway policy</h2><p>Blank values disable the corresponding admission limit.</p></div></div>
          <form className="policy-form" onSubmit={(event) => void policyForm.handleSubmit((values) => policyMutation.mutate(values))(event)}>
            <PolicyNumber control={policyForm.control} name="requestsPerMinute" label="Requests / minute" />
            <PolicyNumber control={policyForm.control} name="concurrentRequests" label="Concurrent requests" />
            <PolicyNumber control={policyForm.control} name="tokensPerDay" label="Tokens / day" />
            <PolicyNumber control={policyForm.control} name="dailySpendUsd" label="Daily spend" prefix="$" step={0.000001} />
            <PolicyNumber control={policyForm.control} name="monthlySpendUsd" label="Monthly spend" prefix="$" step={0.000001} />
            <PolicyNumber control={policyForm.control} name="exactCacheTtlSeconds" label="Exact cache TTL" suffix="sec" />
            <Button type="primary" htmlType="submit" icon={<Save size={15} />} loading={policyMutation.isPending}>Save policy</Button>
          </form>
        </section>
      </> : <Empty description="Create or select a gateway" />}

      <Modal open={gatewayOpen} title="New gateway" footer={null} onCancel={() => setGatewayOpen(false)} destroyOnHidden>
        <form className="modal-form" onSubmit={(event) => void gatewayForm.handleSubmit((values) => createGatewayMutation.mutate(values))(event)}>
          <label><span>Project</span><Controller name="projectId" control={gatewayForm.control} render={({ field }) => <Select {...field} options={(projects.data ?? []).map((item) => ({ value: item.id, label: item.name }))} />} /></label>
          <label><span>Slug</span><Controller name="slug" control={gatewayForm.control} render={({ field }) => <Input {...field} placeholder="production-agents" status={gatewayForm.formState.errors.slug ? "error" : undefined} />} /></label>
          <Button type="primary" htmlType="submit" icon={<Plus size={15} />} loading={createGatewayMutation.isPending}>Create gateway</Button>
        </form>
      </Modal>

      <Modal open={routeOpen} title="Add route" footer={null} onCancel={() => setRouteOpen(false)} width={720} destroyOnHidden>
        <form className="modal-form route-form" onSubmit={(event) => void routeForm.handleSubmit((values) => createRouteMutation.mutate(values))(event)}>
          <label className="span-two"><span>Provider credential</span><Controller name="credentialId" control={routeForm.control} render={({ field }) => <Select {...field} options={projectCredentials.map((item) => ({ value: item.id, label: `${item.name} · ${item.provider} · ${item.adapter}` }))} />} /></label>
          <label className="span-two"><span>Route name</span><Controller name="name" control={routeForm.control} render={({ field }) => <Input {...field} status={routeForm.formState.errors.name ? "error" : undefined} />} /></label>
          <NumberField control={routeForm.control} name="priority" label="Priority" min={0} max={10000} />
          <NumberField control={routeForm.control} name="weight" label="Weight" min={1} max={10000} />
          <NumberField control={routeForm.control} name="maxRetries" label="Max retries" min={0} max={5} />
          <NumberField control={routeForm.control} name="healthFailureThreshold" label="Circuit threshold" min={0} max={100} />
          <NumberField control={routeForm.control} name="healthRecoverySeconds" label="Recovery seconds" min={1} max={3600} />
          <label><span>Enabled</span><Controller name="enabled" control={routeForm.control} render={({ field }) => <Switch checked={field.value} onChange={field.onChange} />} /></label>
          <div className="metadata-editor span-two"><span>Trusted metadata match</span>{metadataFields.fields.map((field, index) => <div className="metadata-row" key={field.id}><Controller name={`metadata.${index}.key`} control={routeForm.control} render={({ field: item }) => <Input {...item} placeholder="environment" />} /><Controller name={`metadata.${index}.value`} control={routeForm.control} render={({ field: item }) => <Input {...item} placeholder="production" />} /><Button type="text" icon={<Trash2 size={14} />} onClick={() => metadataFields.remove(index)} aria-label="Remove metadata condition" /></div>)}<Button type="dashed" icon={<Plus size={14} />} disabled={metadataFields.fields.length >= 16} onClick={() => metadataFields.append({ key: "", value: "" })}>Add condition</Button></div>
          {routeForm.formState.errors.metadata ? <Alert className="span-two" type="error" message="Invalid metadata conditions" /> : null}
          <Button className="modal-submit span-two" type="primary" htmlType="submit" icon={<Plus size={15} />} loading={createRouteMutation.isPending}>Create route</Button>
        </form>
      </Modal>

      <Modal open={Boolean(editingRoute)} title={editingRoute ? `Edit ${editingRoute.name}` : "Edit route"} footer={null} onCancel={() => setEditingRoute(null)} destroyOnHidden>
        <form className="modal-form compact-form" onSubmit={(event) => void editForm.handleSubmit((values) => editRouteMutation.mutate(values))(event)}>
          <NumberField control={editForm.control} name="priority" label="Priority" min={0} max={10000} />
          <NumberField control={editForm.control} name="weight" label="Weight" min={1} max={10000} />
          <label><span>Enabled</span><Controller name="enabled" control={editForm.control} render={({ field }) => <Switch checked={field.value} onChange={field.onChange} />} /></label>
          <Button type="primary" htmlType="submit" icon={<Save size={15} />} loading={editRouteMutation.isPending}>Save route</Button>
        </form>
      </Modal>
    </div>
  );
}

function NumberField<T extends RouteForm | EditRouteForm>({ control, name, label, min, max }: { control: import("react-hook-form").Control<T>; name: import("react-hook-form").Path<T>; label: string; min: number; max: number }) {
  return <label><span>{label}</span><Controller name={name} control={control} render={({ field }) => <InputNumber min={min} max={max} value={field.value as number} onChange={field.onChange} />} /></label>;
}

function PolicyNumber({ control, name, label, prefix, suffix, step = 1 }: { control: import("react-hook-form").Control<PolicyForm>; name: import("react-hook-form").Path<PolicyForm>; label: string; prefix?: string; suffix?: string; step?: number }) {
  return <label><span>{label}</span><Controller name={name} control={control} render={({ field }) => <InputNumber min={step} step={step} prefix={prefix} suffix={suffix} value={field.value as number | null} onChange={field.onChange} placeholder="Unlimited" />} /></label>;
}
