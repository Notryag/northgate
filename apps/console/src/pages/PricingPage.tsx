import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Input, Table } from "antd";
import { Plus } from "lucide-react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";
import { useAuth } from "../auth";
import { createModelPrice, loadModelPrices } from "../api";
import { PageHeader } from "../components/PageHeader";
import { cost, dateTime } from "../format";

function localDateTime(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

const formSchema = z.object({
  provider: z.string().trim().min(1).max(40),
  model: z.string().trim().min(1).max(200),
  inputPrice: z.string().refine((value) => Number.isFinite(Number(value)) && Number(value) >= 0, "Enter a non-negative price"),
  outputPrice: z.string().refine((value) => Number.isFinite(Number(value)) && Number(value) >= 0, "Enter a non-negative price"),
  effectiveFrom: z.string().min(1),
});
type PriceForm = z.infer<typeof formSchema>;

export function PricingPage() {
  const { operatorKey } = useAuth();
  const queryClient = useQueryClient();
  const prices = useQuery({ queryKey: ["model-prices"], queryFn: () => loadModelPrices(operatorKey), enabled: Boolean(operatorKey) });
  const { control, handleSubmit, reset, formState: { errors } } = useForm<PriceForm>({ resolver: zodResolver(formSchema), defaultValues: { provider: "openai", model: "", inputPrice: "", outputPrice: "", effectiveFrom: localDateTime() } });
  const mutation = useMutation({
    mutationFn: (values: PriceForm) => createModelPrice(operatorKey, {
      provider: values.provider.trim(),
      model: values.model.trim(),
      effective_from: new Date(values.effectiveFrom).toISOString(),
      input_microusd_per_million: Math.round(Number(values.inputPrice) * 1_000_000),
      output_microusd_per_million: Math.round(Number(values.outputPrice) * 1_000_000),
    }),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["model-prices"] }); reset({ provider: "openai", model: "", inputPrice: "", outputPrice: "", effectiveFrom: localDateTime() }); },
  });

  return (
    <div className="page-stack">
      <PageHeader title="Pricing" description="Append-only model prices used for request and attempt cost accounting." />
      {prices.error || mutation.error ? <Alert type="error" showIcon message="Pricing operation failed" description={(prices.error ?? mutation.error)?.message} /> : null}
      <section className="form-band">
        <div className="section-heading"><div><h2>Add price version</h2><p>Prices are USD per one million tokens and become effective at the selected time.</p></div></div>
        <form className="pricing-form" onSubmit={(event) => void handleSubmit((values) => mutation.mutate(values))(event)}>
          <label><span>Provider</span><Controller name="provider" control={control} render={({ field }) => <Input {...field} status={errors.provider ? "error" : undefined} />} /></label>
          <label className="wide-field"><span>Model</span><Controller name="model" control={control} render={({ field }) => <Input {...field} status={errors.model ? "error" : undefined} />} /></label>
          <label><span>Input $ / 1M</span><Controller name="inputPrice" control={control} render={({ field }) => <Input {...field} type="number" min="0" step="0.000001" status={errors.inputPrice ? "error" : undefined} />} /></label>
          <label><span>Output $ / 1M</span><Controller name="outputPrice" control={control} render={({ field }) => <Input {...field} type="number" min="0" step="0.000001" status={errors.outputPrice ? "error" : undefined} />} /></label>
          <label><span>Effective from</span><Controller name="effectiveFrom" control={control} render={({ field }) => <Input {...field} type="datetime-local" status={errors.effectiveFrom ? "error" : undefined} />} /></label>
          <Button type="primary" htmlType="submit" icon={<Plus size={15} />} loading={mutation.isPending}>Add price</Button>
        </form>
      </section>
      <section className="work-section table-only">
        <div className="section-heading"><div><h2>Price history</h2><p>{prices.data?.length ?? 0} versioned prices</p></div></div>
        <Table
          size="small"
          rowKey="id"
          loading={prices.isLoading}
          dataSource={prices.data ?? []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          columns={[
            { title: "Provider", dataIndex: "provider", render: (value: string) => <span className="mono">{value}</span> },
            { title: "Model", dataIndex: "model" },
            { title: "Input $ / 1M", dataIndex: "input_microusd_per_million", align: "right", render: cost },
            { title: "Output $ / 1M", dataIndex: "output_microusd_per_million", align: "right", render: cost },
            { title: "Effective from", dataIndex: "effective_from", render: dateTime },
          ]}
          locale={{ emptyText: "No model prices configured" }}
        />
      </section>
    </div>
  );
}
