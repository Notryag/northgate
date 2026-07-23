import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, Modal, Select, Table, Tag } from "antd";
import { Database, KeyRound, Link2, Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../auth";
import {
  createProviderCredential,
  loadGateways,
  loadProjects,
  loadProviderCredentials,
  loadRoutes,
  rotateProviderCredential,
  type ProviderCredential,
} from "../api";
import { MetricStrip } from "../components/MetricStrip";
import { PageHeader } from "../components/PageHeader";
import { numberFormat } from "../format";

const providerSchema = z.object({
  name: z.string().trim().min(1).max(200),
  provider: z.string().trim().min(1).max(40),
  baseUrl: z.string().url().max(2048),
  adapter: z.enum(["openai_compatible", "azure_openai"]),
  apiVersion: z.string().max(512),
  apiKey: z.string().min(1).max(8192),
}).superRefine((value, context) => {
  if (value.adapter === "azure_openai" && !value.apiVersion.trim()) {
    context.addIssue({ code: "custom", message: "Azure OpenAI requires an API version", path: ["apiVersion"] });
  }
});
type ProviderForm = z.infer<typeof providerSchema>;

const rotationSchema = z.object({ apiKey: z.string().min(1).max(8192) });
type RotationForm = z.infer<typeof rotationSchema>;

export function ProvidersPage() {
  const { operatorKey } = useAuth();
  const queryClient = useQueryClient();
  const enabled = Boolean(operatorKey);
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => loadProjects(operatorKey), enabled });
  const credentials = useQuery({ queryKey: ["provider-credentials"], queryFn: () => loadProviderCredentials(operatorKey), enabled });
  const routes = useQuery({ queryKey: ["routes"], queryFn: () => loadRoutes(operatorKey), enabled });
  const gateways = useQuery({ queryKey: ["gateways"], queryFn: () => loadGateways(operatorKey), enabled });
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [rotating, setRotating] = useState<ProviderCredential | null>(null);

  useEffect(() => {
    if (!selectedProjectId && projects.data?.[0]) setSelectedProjectId(projects.data[0].id);
  }, [projects.data, selectedProjectId]);

  const providerForm = useForm<ProviderForm>({ resolver: zodResolver(providerSchema), defaultValues: { name: "", provider: "openai", baseUrl: "https://api.openai.com/v1", adapter: "openai_compatible", apiVersion: "", apiKey: "" } });
  const rotationForm = useForm<RotationForm>({ resolver: zodResolver(rotationSchema), defaultValues: { apiKey: "" } });
  const selectedAdapter = providerForm.watch("adapter");

  const createMutation = useMutation({
    mutationFn: (values: ProviderForm) => createProviderCredential(operatorKey, {
      project_id: selectedProjectId,
      name: values.name.trim(),
      provider: values.provider.trim(),
      base_url: values.baseUrl.trim(),
      adapter: values.adapter,
      adapter_config: values.adapter === "azure_openai" ? { api_version: values.apiVersion.trim() } : {},
      api_key: values.apiKey,
    }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["provider-credentials"] }); setCreateOpen(false); providerForm.reset(); },
  });
  const rotationMutation = useMutation({
    mutationFn: (values: RotationForm) => rotateProviderCredential(operatorKey, rotating!.id, values.apiKey),
    onSuccess: () => { setRotating(null); rotationForm.reset(); },
  });

  const gatewayById = useMemo(() => new Map((gateways.data ?? []).map((item) => [item.id, item])), [gateways.data]);
  const routeReferences = useMemo(() => {
    const references = new Map<string, string[]>();
    for (const route of routes.data ?? []) {
      const gateway = gatewayById.get(route.gateway_id);
      const label = `${gateway?.slug ?? route.gateway_id} / ${route.name}`;
      references.set(route.provider_credential_id, [...(references.get(route.provider_credential_id) ?? []), label]);
    }
    return references;
  }, [gatewayById, routes.data]);
  const projectCredentials = (credentials.data ?? []).filter((item) => item.project_id === selectedProjectId);
  const selectedProject = projects.data?.find((item) => item.id === selectedProjectId);
  const error = projects.error ?? credentials.error ?? routes.error ?? gateways.error;
  const mutationError = createMutation.error ?? rotationMutation.error;

  return (
    <div className="page-stack">
      <PageHeader title="Provider Credentials" description="Manage encrypted upstream credentials, adapters, secret rotation, and route references." actions={<Button type="primary" icon={<Plus size={15} />} disabled={!selectedProjectId} onClick={() => { providerForm.reset({ name: "", provider: "openai", baseUrl: "https://api.openai.com/v1", adapter: "openai_compatible", apiVersion: "", apiKey: "" }); setCreateOpen(true); }}>New credential</Button>} />
      {error || mutationError ? <Alert type="error" showIcon message="Provider operation failed" description={(error ?? mutationError)?.message} /> : null}
      <MetricStrip items={[
        { label: "Credentials", value: numberFormat.format(credentials.data?.length ?? 0), icon: KeyRound },
        { label: "Project credentials", value: numberFormat.format(projectCredentials.length), icon: Database },
        { label: "Referenced", value: numberFormat.format(projectCredentials.filter((item) => (routeReferences.get(item.id)?.length ?? 0) > 0).length), icon: Link2, tone: "healthy" },
        { label: "OpenAI-compatible", value: numberFormat.format(projectCredentials.filter((item) => item.adapter === "openai_compatible").length), icon: ShieldCheck },
        { label: "Azure OpenAI", value: numberFormat.format(projectCredentials.filter((item) => item.adapter === "azure_openai").length), icon: ShieldCheck },
      ]} />
      <section className="gateway-selector">
        <label><span>Project</span><Select value={selectedProjectId || undefined} loading={projects.isLoading} onChange={setSelectedProjectId} options={(projects.data ?? []).map((item) => ({ value: item.id, label: item.name }))} placeholder="Select a project" /></label>
        {selectedProject ? <div className="gateway-identity"><span>Project</span><strong>{selectedProject.name}</strong><code>{selectedProject.id}</code></div> : null}
      </section>
      {selectedProject ? <section className="work-section table-only">
        <div className="section-heading"><div><h2>Encrypted credentials</h2><p>Secret material is write-only and never returned by the control API.</p></div></div>
        <Table<ProviderCredential>
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={projectCredentials}
          scroll={{ x: 1050 }}
          columns={[
            { title: "Credential", dataIndex: "name", render: (value: string, record) => <span><strong>{value}</strong><small className="cell-subtitle mono">{record.id}</small></span> },
            { title: "Provider", dataIndex: "provider", width: 120, render: (value: string) => <span className="mono">{value}</span> },
            { title: "Adapter", dataIndex: "adapter", width: 150, render: (value: string) => <Tag color={value === "azure_openai" ? "blue" : undefined}>{value.replaceAll("_", " ")}</Tag> },
            { title: "Base URL", dataIndex: "base_url", render: (value: string) => <code className="mono wrap-code">{value}</code> },
            { title: "Adapter config", dataIndex: "adapter_config", width: 170, render: (value: Record<string, string>) => Object.entries(value).length ? Object.entries(value).map(([key, item]) => <span key={key}><span className="muted">{key}</span><small className="cell-subtitle mono">{item}</small></span>) : <span className="muted">Default</span> },
            { title: "Routes", width: 220, render: (_: unknown, record) => { const references = routeReferences.get(record.id) ?? []; return references.length ? <div className="tag-list">{references.map((item) => <Tag key={item}>{item}</Tag>)}</div> : <span className="muted">Not referenced</span>; } },
            { title: "", width: 110, render: (_: unknown, record) => <Button size="small" icon={<RefreshCw size={13} />} onClick={() => { rotationForm.reset(); setRotating(record); }}>Rotate secret</Button> },
          ]}
          locale={{ emptyText: "No provider credentials in this project" }}
        />
      </section> : <Empty description="Create or select a project" />}

      <Modal open={createOpen} title="New provider credential" footer={null} width={680} onCancel={() => setCreateOpen(false)} destroyOnHidden>
        <form className="modal-form compact-form" onSubmit={(event) => void providerForm.handleSubmit((values) => createMutation.mutate(values))(event)}>
          <label><span>Name</span><Input {...providerForm.register("name")} /></label>
          <label><span>Provider label</span><Input {...providerForm.register("provider")} /></label>
          <label className="span-two"><span>Base URL</span><Input {...providerForm.register("baseUrl")} /></label>
          <label><span>Adapter</span><Controller name="adapter" control={providerForm.control} render={({ field }) => <Select {...field} options={[{ value: "openai_compatible", label: "OpenAI-compatible" }, { value: "azure_openai", label: "Azure OpenAI" }]} />} /></label>
          {selectedAdapter === "azure_openai" ? <label><span>API version</span><Input {...providerForm.register("apiVersion")} placeholder="2025-04-01-preview" /></label> : <div />}
          <label className="span-two"><span>API key</span><Input.Password {...providerForm.register("apiKey")} autoComplete="new-password" /></label>
          <Button className="modal-submit span-two" type="primary" htmlType="submit" loading={createMutation.isPending}>Create credential</Button>
        </form>
      </Modal>
      <Modal open={Boolean(rotating)} title={rotating ? `Rotate ${rotating.name}` : "Rotate secret"} footer={null} onCancel={() => setRotating(null)} destroyOnHidden>
        <form className="modal-form" onSubmit={(event) => void rotationForm.handleSubmit((values) => rotationMutation.mutate(values))(event)}>
          <Alert type="warning" showIcon message="The existing secret cannot be recovered after rotation" />
          <label><span>New API key</span><Input.Password {...rotationForm.register("apiKey")} autoComplete="new-password" /></label>
          <Button className="modal-submit" type="primary" htmlType="submit" icon={<RefreshCw size={14} />} loading={rotationMutation.isPending}>Rotate secret</Button>
        </form>
      </Modal>
    </div>
  );
}
