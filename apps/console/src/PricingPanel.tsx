import { useState, type FormEvent } from "react";
import { Plus } from "lucide-react";
import type { ModelPrice, ModelPriceCreateInput } from "./api";

function localDateTime(): string {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 16);
}

function dollars(value: number): string {
  return `$${(value / 1_000_000).toFixed(6)}`;
}

function effectiveTime(value: string): string {
  return new Date(value).toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface PricingPanelProps {
  prices: ModelPrice[];
  busy: boolean;
  error: string;
  onCreate: (input: ModelPriceCreateInput) => Promise<void>;
}

export function PricingPanel({ prices, busy, error, onCreate }: PricingPanelProps) {
  const [provider, setProvider] = useState("openai");
  const [model, setModel] = useState("");
  const [inputPrice, setInputPrice] = useState("");
  const [outputPrice, setOutputPrice] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState(localDateTime);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const inputMicrousd = Math.round(Number(inputPrice) * 1_000_000);
    const outputMicrousd = Math.round(Number(outputPrice) * 1_000_000);
    await onCreate({
      provider,
      model,
      effective_from: new Date(effectiveFrom).toISOString(),
      input_microusd_per_million: inputMicrousd,
      output_microusd_per_million: outputMicrousd,
    });
  }

  return (
    <section className="table-section">
      <div className="section-heading">
        <div>
          <h2>Model pricing</h2>
          <p>{`${prices.length} historical prices`}</p>
        </div>
      </div>
      <form className="pricing-form" onSubmit={(event) => void submit(event)}>
        <div className="field">
          <label htmlFor="price-provider">Provider</label>
          <input id="price-provider" value={provider} onChange={(event) => setProvider(event.target.value)} maxLength={40} required />
        </div>
        <div className="field">
          <label htmlFor="price-model">Model</label>
          <input id="price-model" value={model} onChange={(event) => setModel(event.target.value)} maxLength={200} required />
        </div>
        <div className="field">
          <label htmlFor="input-price">Input $ / 1M</label>
          <input id="input-price" type="number" min="0" step="0.000001" value={inputPrice} onChange={(event) => setInputPrice(event.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="output-price">Output $ / 1M</label>
          <input id="output-price" type="number" min="0" step="0.000001" value={outputPrice} onChange={(event) => setOutputPrice(event.target.value)} required />
        </div>
        <div className="field">
          <label htmlFor="effective-from">Effective from</label>
          <input id="effective-from" type="datetime-local" value={effectiveFrom} onChange={(event) => setEffectiveFrom(event.target.value)} required />
        </div>
        <button className="button primary pricing-submit" type="submit" disabled={busy}>
          <Plus size={15} aria-hidden="true" />{busy ? "Adding" : "Add price"}
        </button>
        <p className="pricing-error" role="status">{error}</p>
      </form>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Provider</th><th>Model</th><th>Input $ / 1M</th><th>Output $ / 1M</th><th>Effective from</th></tr></thead>
          <tbody>
            {prices.length ? prices.map((price) => (
              <tr key={price.id}>
                <td>{price.provider}</td>
                <td className="model-name">{price.model}</td>
                <td>{dollars(price.input_microusd_per_million)}</td>
                <td>{dollars(price.output_microusd_per_million)}</td>
                <td>{effectiveTime(price.effective_from)}</td>
              </tr>
            )) : <tr><td colSpan={5} className="empty">No model prices configured</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}
