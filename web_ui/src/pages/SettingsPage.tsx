import { Save, ShieldCheck } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { api, toApiError } from "../api/client";
import type { ApiError, ProviderName, SettingsStatus, WebSettingsPayload } from "../api/types";
import ErrorNotice from "../components/ErrorNotice";

const PROVIDERS: ProviderName[] = ["openai", "deepseek", "kimi", "qwen", "claude"];

type SettingsForm = {
  reproduceProvider: ProviderName;
  reproduceModel: string;
  reproduceApiKey: string;
  reproduceBaseUrl: string;
  evaluationProvider: ProviderName;
  evaluationModel: string;
  evaluationApiKey: string;
  evaluationBaseUrl: string;
  evaluationFallbackModels: string;
};

const emptyForm: SettingsForm = {
  reproduceProvider: "openai",
  reproduceModel: "",
  reproduceApiKey: "",
  reproduceBaseUrl: "",
  evaluationProvider: "openai",
  evaluationModel: "",
  evaluationApiKey: "",
  evaluationBaseUrl: "",
  evaluationFallbackModels: "",
};

export default function SettingsPage() {
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [form, setForm] = useState<SettingsForm>(emptyForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let ignore = false;
    api
      .getSettingsStatus()
      .then((nextStatus) => {
        if (ignore) return;
        setStatus(nextStatus);
      })
      .catch((err) => setError(toApiError(err)))
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setSaved(false);
    setError(null);

    const payload: WebSettingsPayload = {
      reproduce: {
        provider: form.reproduceProvider,
        model: form.reproduceModel.trim(),
        api_key: form.reproduceApiKey,
        base_url: form.reproduceBaseUrl.trim(),
      },
      evaluation: {
        provider: form.evaluationProvider,
        model: form.evaluationModel.trim(),
        api_key: form.evaluationApiKey,
        base_url: form.evaluationBaseUrl.trim(),
        fallback_models: form.evaluationFallbackModels
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      },
    };

    try {
      const nextStatus = await api.saveSettings(payload);
      setStatus(nextStatus);
      setForm((current) => ({
        ...current,
        reproduceApiKey: "",
        evaluationApiKey: "",
      }));
      setSaved(true);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="page-stack">
      <div className="page-header">
        <div>
          <h1>Settings</h1>
          <p>Configure local model providers for reproduction and evaluation.</p>
        </div>
        <div className={`config-pill ${status?.configured ? "ready" : "missing"}`}>
          <ShieldCheck size={16} />
          {loading ? "checking" : status?.configured ? "configured" : "unconfigured"}
        </div>
      </div>

      <ErrorNotice error={error} />
      {saved ? <div className="success-note">Settings saved. API key inputs were cleared.</div> : null}

      <form className="panel form-grid" onSubmit={handleSubmit}>
        <fieldset>
          <legend>Reproduce</legend>
          <ProviderSelect
            label="provider"
            value={form.reproduceProvider}
            onChange={(value) => setForm({ ...form, reproduceProvider: value })}
          />
          <TextField
            label="model"
            value={form.reproduceModel}
            onChange={(value) => setForm({ ...form, reproduceModel: value })}
            required
          />
          <TextField
            label="api_key"
            value={form.reproduceApiKey}
            onChange={(value) => setForm({ ...form, reproduceApiKey: value })}
            required
            type="password"
            placeholder={status?.reproduce?.has_api_key ? "Saved key exists; enter a key to save" : ""}
          />
          <TextField
            label="base_url"
            value={form.reproduceBaseUrl}
            onChange={(value) => setForm({ ...form, reproduceBaseUrl: value })}
          />
          <div className="field-meta">
            has_api_key: {status?.reproduce?.has_api_key ? "true" : "false"}
          </div>
        </fieldset>

        <fieldset>
          <legend>Evaluation</legend>
          <ProviderSelect
            label="provider"
            value={form.evaluationProvider}
            onChange={(value) => setForm({ ...form, evaluationProvider: value })}
          />
          <TextField
            label="model"
            value={form.evaluationModel}
            onChange={(value) => setForm({ ...form, evaluationModel: value })}
            required
          />
          <TextField
            label="api_key"
            value={form.evaluationApiKey}
            onChange={(value) => setForm({ ...form, evaluationApiKey: value })}
            required
            type="password"
            placeholder={status?.evaluation?.has_api_key ? "Saved key exists; enter a key to save" : ""}
          />
          <TextField
            label="base_url"
            value={form.evaluationBaseUrl}
            onChange={(value) => setForm({ ...form, evaluationBaseUrl: value })}
          />
          <TextField
            label="fallback_models"
            value={form.evaluationFallbackModels}
            onChange={(value) => setForm({ ...form, evaluationFallbackModels: value })}
            placeholder="model-a, model-b"
          />
          <div className="field-meta">
            has_api_key: {status?.evaluation?.has_api_key ? "true" : "false"}
          </div>
        </fieldset>

        <div className="form-actions">
          <button className="primary-button" disabled={saving} type="submit">
            <Save size={16} />
            {saving ? "Saving" : "Save Settings"}
          </button>
        </div>
      </form>
    </section>
  );
}

function ProviderSelect({
  label,
  value,
  onChange,
}: {
  label: string;
  value: ProviderName;
  onChange: (value: ProviderName) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value as ProviderName)}>
        {PROVIDERS.map((provider) => (
          <option key={provider} value={provider}>
            {provider}
          </option>
        ))}
      </select>
    </label>
  );
}

function TextField({
  label,
  value,
  onChange,
  required,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  type?: string;
  placeholder?: string;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        required={required}
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
