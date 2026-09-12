import { Save, ShieldCheck } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { api, toApiError } from "../api/client";
import type {
  ApiError,
  ProviderName,
  ProviderRegistryResponse,
  SettingsView,
  SettingsStatus,
  WebSettingsPayload,
} from "../api/types";
import ErrorNotice from "../components/ErrorNotice";

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
  reproduceProvider: "",
  reproduceModel: "",
  reproduceApiKey: "",
  reproduceBaseUrl: "",
  evaluationProvider: "",
  evaluationModel: "",
  evaluationApiKey: "",
  evaluationBaseUrl: "",
  evaluationFallbackModels: "",
};

function registeredModels(
  registry: ProviderRegistryResponse,
  providerId: ProviderName,
): string[] {
  return (
    registry.providers.find((provider) => provider.provider_id === providerId)?.models.map(
      (model) => model.model_id,
    ) ?? []
  );
}

function resolveSelection(
  registry: ProviderRegistryResponse,
  settings: SettingsView,
  section: "reproduce" | "evaluation",
): { provider: ProviderName; model: string } {
  const saved = settings[section];
  const firstProvider = registry.providers[0];
  const provider = registry.providers.some((item) => item.provider_id === saved.provider)
    ? saved.provider
    : (firstProvider?.provider_id ?? "");
  const models = registeredModels(registry, provider);
  return {
    provider,
    model: models.includes(saved.model) ? saved.model : (models[0] ?? ""),
  };
}

export default function SettingsPage() {
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [registry, setRegistry] = useState<ProviderRegistryResponse | null>(null);
  const [form, setForm] = useState<SettingsForm>(emptyForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let ignore = false;
    Promise.all([api.getSettings(), api.getProviders()])
      .then(([nextSettings, nextRegistry]) => {
        if (ignore) return;
        setStatus(nextSettings);
        setRegistry(nextRegistry);
        const reproduce = resolveSelection(nextRegistry, nextSettings, "reproduce");
        const evaluation = resolveSelection(nextRegistry, nextSettings, "evaluation");
        const evaluationModels = registeredModels(nextRegistry, evaluation.provider);
        setForm({
          reproduceProvider: reproduce.provider,
          reproduceModel: reproduce.model,
          reproduceApiKey: "",
          reproduceBaseUrl: nextSettings.configured ? nextSettings.reproduce.base_url : "",
          evaluationProvider: evaluation.provider,
          evaluationModel: evaluation.model,
          evaluationApiKey: "",
          evaluationBaseUrl: nextSettings.configured ? nextSettings.evaluation.base_url : "",
          evaluationFallbackModels: nextSettings.configured
            ? nextSettings.evaluation.fallback_models
                .filter((model) => evaluationModels.includes(model))
                .join(",")
            : "",
        });
      })
      .catch((err) => setError(toApiError(err)))
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, []);

  function modelsFor(providerId: ProviderName): string[] {
    return registry ? registeredModels(registry, providerId) : [];
  }

  function invalidSelectionError(message: string): ApiError {
    return {
      status: 0,
      code: "invalid_provider_selection",
      message,
      details: {},
    };
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fallbackModels = form.evaluationFallbackModels
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const reproduceModels = modelsFor(form.reproduceProvider);
    const evaluationModels = modelsFor(form.evaluationProvider);
    if (!registry) {
      setError(invalidSelectionError("Provider Registry is unavailable."));
      return;
    }
    if (!reproduceModels.includes(form.reproduceModel)) {
      setError(invalidSelectionError("Select a registered reproduction model."));
      return;
    }
    if (
      !evaluationModels.includes(form.evaluationModel) ||
      fallbackModels.some((model) => !evaluationModels.includes(model))
    ) {
      setError(invalidSelectionError("Select only registered evaluation models."));
      return;
    }
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
        fallback_models: fallbackModels,
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
            providers={registry?.providers.map((provider) => provider.provider_id) ?? []}
            onChange={(value) =>
              setForm((current) => ({
                ...current,
                reproduceProvider: value,
                reproduceModel: modelsFor(value)[0] ?? "",
              }))
            }
          />
          <ModelSelect
            label="model"
            value={form.reproduceModel}
            models={modelsFor(form.reproduceProvider)}
            onChange={(value) => setForm({ ...form, reproduceModel: value })}
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
            required
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
            providers={registry?.providers.map((provider) => provider.provider_id) ?? []}
            onChange={(value) =>
              setForm((current) => ({
                ...current,
                evaluationProvider: value,
                evaluationModel: modelsFor(value)[0] ?? "",
                evaluationFallbackModels: "",
              }))
            }
          />
          <ModelSelect
            label="model"
            value={form.evaluationModel}
            models={modelsFor(form.evaluationProvider)}
            onChange={(value) => setForm({ ...form, evaluationModel: value })}
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
            required
          />
          <MultiModelSelect
            label="fallback_models"
            values={form.evaluationFallbackModels.split(",").filter(Boolean)}
            models={modelsFor(form.evaluationProvider).filter(
              (model) => model !== form.evaluationModel,
            )}
            onChange={(values) =>
              setForm({ ...form, evaluationFallbackModels: values.join(",") })
            }
          />
          <div className="field-meta">
            has_api_key: {status?.evaluation?.has_api_key ? "true" : "false"}
          </div>
        </fieldset>

        <div className="form-actions">
          <button
            className="primary-button"
            disabled={saving || loading || !registry || registry.providers.length === 0}
            type="submit"
          >
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
  providers,
  onChange,
}: {
  label: string;
  value: ProviderName;
  providers: ProviderName[];
  onChange: (value: ProviderName) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <select required value={value} onChange={(event) => onChange(event.target.value)}>
        {providers.map((provider) => (
          <option key={provider} value={provider}>
            {provider}
          </option>
        ))}
      </select>
    </label>
  );
}

function ModelSelect({
  label,
  value,
  models,
  onChange,
}: {
  label: string;
  value: string;
  models: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <select required value={value} onChange={(event) => onChange(event.target.value)}>
        {models.map((model) => (
          <option key={model} value={model}>
            {model}
          </option>
        ))}
      </select>
    </label>
  );
}

function MultiModelSelect({
  label,
  values,
  models,
  onChange,
}: {
  label: string;
  values: string[];
  models: string[];
  onChange: (values: string[]) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <select
        multiple
        value={values}
        onChange={(event) =>
          onChange(Array.from(event.target.selectedOptions, (option) => option.value))
        }
      >
        {models.map((model) => (
          <option key={model} value={model}>
            {model}
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
