import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Input, Modal, Popconfirm, Select, Table, Tag, message } from "antd";
import { Building2, Copy, FolderKanban, KeyRound, Plus, ShieldOff } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Controller, useFieldArray, useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../auth";
import {
  createApplicationKey,
  createOrganization,
  createProject,
  loadApplicationKeys,
  loadOrganizations,
  loadProjects,
  revokeApplicationKey,
  type ApplicationKey,
  type CreatedApplicationKey,
} from "../api";
import { MetricStrip } from "../components/MetricStrip";
import { PageHeader } from "../components/PageHeader";
import { StatusTag } from "../components/StatusTag";
import { numberFormat } from "../format";

const namedSchema = z.object({ name: z.string().trim().min(1).max(200) });
type NamedForm = z.infer<typeof namedSchema>;

const projectSchema = namedSchema.extend({ organizationId: z.string().min(1) });
type ProjectForm = z.infer<typeof projectSchema>;

const applicationSchema = z.object({
  name: z.string().trim().min(1).max(200),
  metadataRoutingMode: z.enum(["trusted", "legacy"]),
  allowedMetadataKeys: z.string(),
  fixedMetadata: z.array(z.object({ key: z.string().max(64), value: z.string().max(256) })).max(16),
}).superRefine((value, context) => {
  const callerKeys = value.allowedMetadataKeys.split(",").map((item) => item.trim()).filter(Boolean);
  if (new Set(callerKeys).size !== callerKeys.length || callerKeys.some((key) => !/^[A-Za-z0-9_.-]{1,64}$/.test(key))) {
    context.addIssue({ code: "custom", message: "Use unique comma-separated metadata keys", path: ["allowedMetadataKeys"] });
  }
  const fixedKeys = new Set<string>();
  value.fixedMetadata.forEach((item, index) => {
    if (!item.key && !item.value) return;
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(item.key) || item.key.startsWith("northgate.") || !item.value) {
      context.addIssue({ code: "custom", message: "Complete a valid non-reserved key and value", path: ["fixedMetadata", index] });
    } else if (fixedKeys.has(item.key) || callerKeys.includes(item.key)) {
      context.addIssue({ code: "custom", message: "Metadata keys must be unique across both classes", path: ["fixedMetadata", index, "key"] });
    }
    fixedKeys.add(item.key);
  });
});
type ApplicationForm = z.infer<typeof applicationSchema>;

export function ApplicationsPage() {
  const { operatorKey } = useAuth();
  const queryClient = useQueryClient();
  const enabled = Boolean(operatorKey);
  const organizations = useQuery({ queryKey: ["organizations"], queryFn: () => loadOrganizations(operatorKey), enabled });
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => loadProjects(operatorKey), enabled });
  const applicationKeys = useQuery({ queryKey: ["application-keys"], queryFn: () => loadApplicationKeys(operatorKey), enabled });
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [organizationOpen, setOrganizationOpen] = useState(false);
  const [projectOpen, setProjectOpen] = useState(false);
  const [applicationOpen, setApplicationOpen] = useState(false);
  const [issuedKey, setIssuedKey] = useState<CreatedApplicationKey | null>(null);

  useEffect(() => {
    if (!selectedProjectId && projects.data?.[0]) setSelectedProjectId(projects.data[0].id);
  }, [projects.data, selectedProjectId]);

  const organizationForm = useForm<NamedForm>({ resolver: zodResolver(namedSchema), defaultValues: { name: "" } });
  const projectForm = useForm<ProjectForm>({ resolver: zodResolver(projectSchema), defaultValues: { organizationId: "", name: "" } });
  const applicationForm = useForm<ApplicationForm>({ resolver: zodResolver(applicationSchema), defaultValues: { name: "", metadataRoutingMode: "trusted", allowedMetadataKeys: "tenant_id, user_id, run_id", fixedMetadata: [{ key: "", value: "" }] } });
  const fixedMetadataFields = useFieldArray({ control: applicationForm.control, name: "fixedMetadata" });

  const createOrganizationMutation = useMutation({
    mutationFn: (values: NamedForm) => createOrganization(operatorKey, { name: values.name.trim() }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["organizations"] }); setOrganizationOpen(false); organizationForm.reset(); },
  });
  const createProjectMutation = useMutation({
    mutationFn: (values: ProjectForm) => createProject(operatorKey, { organization_id: values.organizationId, name: values.name.trim() }),
    onSuccess: async (created) => { await queryClient.invalidateQueries({ queryKey: ["projects"] }); setSelectedProjectId(created.id); setProjectOpen(false); projectForm.reset(); },
  });
  const createApplicationMutation = useMutation({
    mutationFn: (values: ApplicationForm) => createApplicationKey(operatorKey, {
      project_id: selectedProjectId,
      name: values.name.trim(),
      metadata_routing_mode: values.metadataRoutingMode,
      allowed_metadata_keys: values.allowedMetadataKeys.split(",").map((item) => item.trim()).filter(Boolean),
      fixed_metadata: Object.fromEntries(values.fixedMetadata.filter((item) => item.key && item.value).map((item) => [item.key, item.value])),
    }),
    onSuccess: async (created) => { await queryClient.invalidateQueries({ queryKey: ["application-keys"] }); setApplicationOpen(false); setIssuedKey(created); applicationForm.reset(); },
  });
  const revokeMutation = useMutation({
    mutationFn: (keyId: string) => revokeApplicationKey(operatorKey, keyId),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["application-keys"] }); },
  });

  const projectById = useMemo(() => new Map((projects.data ?? []).map((item) => [item.id, item])), [projects.data]);
  const organizationById = useMemo(() => new Map((organizations.data ?? []).map((item) => [item.id, item])), [organizations.data]);
  const projectKeys = (applicationKeys.data ?? []).filter((item) => item.project_id === selectedProjectId);
  const selectedProject = projectById.get(selectedProjectId);
  const error = organizations.error ?? projects.error ?? applicationKeys.error;
  const mutationError = createOrganizationMutation.error ?? createProjectMutation.error ?? createApplicationMutation.error ?? revokeMutation.error;

  return (
    <div className="page-stack">
      <PageHeader
        title="Applications"
        description="Manage ownership boundaries, application credentials, and trusted metadata binding."
        actions={<><Button icon={<Building2 size={15} />} onClick={() => { organizationForm.reset(); setOrganizationOpen(true); }}>New organization</Button><Button icon={<FolderKanban size={15} />} disabled={!organizations.data?.length} onClick={() => { projectForm.reset({ organizationId: organizations.data?.[0]?.id ?? "", name: "" }); setProjectOpen(true); }}>New project</Button></>}
      />
      {error || mutationError ? <Alert type="error" showIcon message="Application operation failed" description={(error ?? mutationError)?.message} /> : null}
      <MetricStrip items={[
        { label: "Organizations", value: numberFormat.format(organizations.data?.length ?? 0), icon: Building2 },
        { label: "Projects", value: numberFormat.format(projects.data?.length ?? 0), icon: FolderKanban },
        { label: "Application keys", value: numberFormat.format(applicationKeys.data?.length ?? 0), icon: KeyRound },
        { label: "Active keys", value: numberFormat.format((applicationKeys.data ?? []).filter((item) => item.revoked_at == null).length), icon: KeyRound, tone: "healthy" },
        { label: "Revoked keys", value: numberFormat.format((applicationKeys.data ?? []).filter((item) => item.revoked_at != null).length), icon: ShieldOff },
      ]} />
      <section className="gateway-selector">
        <label><span>Project</span><Select value={selectedProjectId || undefined} loading={projects.isLoading} onChange={setSelectedProjectId} options={(projects.data ?? []).map((item) => ({ value: item.id, label: `${organizationById.get(item.organization_id)?.name ?? "Organization"} / ${item.name}` }))} placeholder="Select a project" /></label>
        {selectedProject ? <div className="gateway-identity"><span>Organization</span><strong>{organizationById.get(selectedProject.organization_id)?.name ?? selectedProject.organization_id}</strong><code>{selectedProject.id}</code></div> : null}
      </section>
      {selectedProject ? <section className="work-section table-only">
        <div className="section-heading"><div><h2>Application keys</h2><p>Caller metadata is stored for correlation; only fixed metadata affects trusted routing.</p></div><Button type="primary" icon={<Plus size={15} />} onClick={() => { applicationForm.reset({ name: "", metadataRoutingMode: "trusted", allowedMetadataKeys: "tenant_id, user_id, run_id", fixedMetadata: [{ key: "", value: "" }] }); setApplicationOpen(true); }}>New application key</Button></div>
        <Table<ApplicationKey>
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={projectKeys}
          scroll={{ x: 1000 }}
          columns={[
            { title: "Application", dataIndex: "name", render: (value: string, record) => <span><strong>{value}</strong><small className="cell-subtitle mono">{record.id}</small></span> },
            { title: "Routing trust", dataIndex: "metadata_routing_mode", width: 120, render: (value: string) => <Tag color={value === "trusted" ? "green" : "gold"}>{value}</Tag> },
            { title: "Caller metadata", dataIndex: "allowed_metadata_keys", render: (value: string[]) => value.length ? <div className="tag-list">{value.map((item) => <Tag key={item}>{item}</Tag>)}</div> : <span className="muted">None</span> },
            { title: "Fixed metadata", dataIndex: "fixed_metadata", render: (value: Record<string, string>) => Object.keys(value).length ? <div className="tag-list">{Object.keys(value).map((item) => <Tag key={item}>{item}</Tag>)}</div> : <span className="muted">None</span> },
            { title: "Created", dataIndex: "created_at", width: 170, render: (value: string) => new Date(value).toLocaleString() },
            { title: "Status", dataIndex: "revoked_at", width: 100, render: (value: string | null) => <StatusTag value={value ? "revoked" : "ready"} /> },
            { title: "", width: 90, render: (_: unknown, record) => record.revoked_at ? null : <Popconfirm title="Revoke this application key?" description="Existing clients using it will receive 401 responses." okText="Revoke" okButtonProps={{ danger: true }} onConfirm={() => revokeMutation.mutate(record.id)}><Button danger size="small" icon={<ShieldOff size={13} />}>Revoke</Button></Popconfirm> },
          ]}
          locale={{ emptyText: "No application keys in this project" }}
        />
      </section> : <Empty description="Create or select a project" />}

      <Modal open={organizationOpen} title="New organization" footer={null} onCancel={() => setOrganizationOpen(false)} destroyOnHidden>
        <form className="modal-form" onSubmit={(event) => void organizationForm.handleSubmit((values) => createOrganizationMutation.mutate(values))(event)}><label><span>Name</span><Input {...organizationForm.register("name")} autoFocus /></label><Button className="modal-submit" type="primary" htmlType="submit" loading={createOrganizationMutation.isPending}>Create organization</Button></form>
      </Modal>
      <Modal open={projectOpen} title="New project" footer={null} onCancel={() => setProjectOpen(false)} destroyOnHidden>
        <form className="modal-form" onSubmit={(event) => void projectForm.handleSubmit((values) => createProjectMutation.mutate(values))(event)}><label><span>Organization</span><Controller name="organizationId" control={projectForm.control} render={({ field }) => <Select {...field} options={(organizations.data ?? []).map((item) => ({ value: item.id, label: item.name }))} />} /></label><label><span>Name</span><Input {...projectForm.register("name")} /></label><Button className="modal-submit" type="primary" htmlType="submit" loading={createProjectMutation.isPending}>Create project</Button></form>
      </Modal>
      <Modal open={applicationOpen} title="New application key" footer={null} width={680} onCancel={() => setApplicationOpen(false)} destroyOnHidden>
        <form className="modal-form" onSubmit={(event) => void applicationForm.handleSubmit((values) => createApplicationMutation.mutate(values))(event)}>
          <label><span>Name</span><Input {...applicationForm.register("name")} /></label>
          <label><span>Metadata routing</span><Controller name="metadataRoutingMode" control={applicationForm.control} render={({ field }) => <Select {...field} options={[{ value: "trusted", label: "Trusted" }, { value: "legacy", label: "Legacy caller routing" }]} />} /></label>
          <label><span>Caller metadata keys</span><Input {...applicationForm.register("allowedMetadataKeys")} placeholder="tenant_id, user_id, run_id" status={applicationForm.formState.errors.allowedMetadataKeys ? "error" : undefined} /></label>
          <div className="metadata-editor"><span>Fixed routing metadata</span>{fixedMetadataFields.fields.map((field, index) => <div className="metadata-row" key={field.id}><Input placeholder="key" {...applicationForm.register(`fixedMetadata.${index}.key`)} /><Input placeholder="value" {...applicationForm.register(`fixedMetadata.${index}.value`)} /><Button type="text" danger icon={<ShieldOff size={14} />} aria-label="Remove metadata" onClick={() => fixedMetadataFields.remove(index)} /></div>)}<Button type="dashed" icon={<Plus size={14} />} disabled={fixedMetadataFields.fields.length >= 16} onClick={() => fixedMetadataFields.append({ key: "", value: "" })}>Add fixed metadata</Button></div>
          <Button className="modal-submit" type="primary" htmlType="submit" loading={createApplicationMutation.isPending}>Issue key</Button>
        </form>
      </Modal>
      <Modal open={Boolean(issuedKey)} title="Application key issued" footer={<Button type="primary" onClick={() => setIssuedKey(null)}>Done</Button>} closable={false} maskClosable={false} destroyOnHidden>
        <Alert type="warning" showIcon message="This value is shown once" />
        <div className="secret-output"><code>{issuedKey?.key}</code><Button icon={<Copy size={14} />} onClick={() => { if (issuedKey?.key) void navigator.clipboard.writeText(issuedKey.key).then(() => message.success("Application key copied")); }} aria-label="Copy application key" /></div>
      </Modal>
    </div>
  );
}
