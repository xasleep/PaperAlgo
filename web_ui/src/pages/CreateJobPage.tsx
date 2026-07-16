import { FormEvent, useState } from "react";
import { Send } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api, toApiError } from "../api/client";
import type { ApiError, ConsoleOutput, DomainName, EvalType } from "../api/types";
import ErrorNotice from "../components/ErrorNotice";

export default function CreateJobPage() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [paperName, setPaperName] = useState("");
  const [domain, setDomain] = useState<DomainName>("statistics");
  const [evalType, setEvalType] = useState<EvalType>("ref_free");
  const [generatedN, setGeneratedN] = useState(8);
  const [autoRefine, setAutoRefine] = useState(true);
  const [maxRepairRounds, setMaxRepairRounds] = useState(3);
  const [consoleOutput, setConsoleOutput] = useState<ConsoleOutput>("quiet");
  const [skipMineru, setSkipMineru] = useState(false);
  const [pdfMarkdownPath, setPdfMarkdownPath] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    const validationError = validateJobForm({
      file,
      generatedN,
      maxRepairRounds,
      skipMineru,
      pdfMarkdownPath,
    });
    if (validationError) {
      setError(validationError);
      return;
    }

    const formData = new FormData();
    formData.append("file", file as File);
    formData.append("paper_name", paperName);
    formData.append("domain", domain);
    formData.append("eval_type", evalType);
    formData.append("generated_n", String(generatedN));
    formData.append("auto_refine", String(autoRefine));
    formData.append("max_repair_rounds", String(maxRepairRounds));
    formData.append("console_output", consoleOutput);
    formData.append("skip_mineru", String(skipMineru));
    formData.append("pdf_markdown_path", pdfMarkdownPath);

    setSubmitting(true);
    setError(null);
    try {
      const response = await api.createJob(formData);
      navigate(`/jobs/${encodeURIComponent(response.job_id)}`);
    } catch (err) {
      const apiError = toApiError(err);
      setError(apiError);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="page-stack">
      <div className="page-header">
        <div>
          <h1>Create Job</h1>
          <p>Upload a paper PDF and start a local pipeline run.</p>
        </div>
      </div>

      <ErrorNotice error={error} />
      {error?.code === "settings_not_configured" ? (
        <div className="success-note">
          API settings are required before creating jobs.
          <Link className="inline-link" to="/settings">
            Open Settings
          </Link>
        </div>
      ) : null}

      <form className="panel form-grid single" onSubmit={handleSubmit}>
        <fieldset>
          <legend>Input</legend>
          <label>
            <span>PDF file</span>
            <input
              accept="application/pdf,.pdf"
              required
              type="file"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </label>
          <TextField label="paper_name" value={paperName} onChange={setPaperName} />
          <label>
            <span>domain</span>
            <select value={domain} onChange={(event) => setDomain(event.target.value as DomainName)}>
              <option value="statistics">statistics</option>
              <option value="general">general</option>
            </select>
          </label>
          <label>
            <span>eval_type</span>
            <select value={evalType} onChange={(event) => setEvalType(event.target.value as EvalType)}>
              <option value="ref_free">ref_free</option>
              <option value="ref_based">ref_based</option>
            </select>
          </label>
        </fieldset>

        <fieldset>
          <legend>Run options</legend>
          <label>
            <span>generated_n</span>
            <input
              min={1}
              max={32}
              type="number"
              value={generatedN}
              onChange={(event) => setGeneratedN(Number(event.target.value))}
            />
          </label>
          <label>
            <span>max_repair_rounds</span>
            <input
              min={0}
              max={10}
              type="number"
              value={maxRepairRounds}
              onChange={(event) => setMaxRepairRounds(Number(event.target.value))}
            />
          </label>
          <label>
            <span>console_output</span>
            <select
              value={consoleOutput}
              onChange={(event) => setConsoleOutput(event.target.value as ConsoleOutput)}
            >
              <option value="quiet">quiet</option>
              <option value="progress">progress</option>
              <option value="full">full</option>
            </select>
          </label>
          <label className="inline-check">
            <input
              checked={autoRefine}
              type="checkbox"
              onChange={(event) => setAutoRefine(event.target.checked)}
            />
            <span>auto_refine</span>
          </label>
          <label className="inline-check">
            <input
              checked={skipMineru}
              type="checkbox"
              onChange={(event) => setSkipMineru(event.target.checked)}
            />
            <span>skip_mineru</span>
          </label>
          <TextField
            label="pdf_markdown_path"
            value={pdfMarkdownPath}
            onChange={setPdfMarkdownPath}
            disabled={!skipMineru}
          />
        </fieldset>

        <div className="form-actions">
          <button className="primary-button" disabled={submitting} type="submit">
            <Send size={16} />
            {submitting ? "Creating" : "Create Job"}
          </button>
        </div>
      </form>
    </section>
  );
}

function validateJobForm({
  file,
  generatedN,
  maxRepairRounds,
  skipMineru,
  pdfMarkdownPath,
}: {
  file: File | null;
  generatedN: number;
  maxRepairRounds: number;
  skipMineru: boolean;
  pdfMarkdownPath: string;
}): ApiError | null {
  if (!file) {
    return frontendError("missing_pdf", "Select a PDF file before creating a job.");
  }
  const hasPdfName = file.name.toLowerCase().endsWith(".pdf");
  const hasPdfMime = !file.type || file.type === "application/pdf";
  if (!hasPdfName || !hasPdfMime) {
    return frontendError("invalid_pdf", "Select a file with a .pdf name and PDF content type.");
  }
  if (!Number.isInteger(generatedN) || generatedN < 1 || generatedN > 32) {
    return frontendError("invalid_generated_n", "generated_n must be an integer between 1 and 32.");
  }
  if (!Number.isInteger(maxRepairRounds) || maxRepairRounds < 0 || maxRepairRounds > 10) {
    return frontendError(
      "invalid_max_repair_rounds",
      "max_repair_rounds must be an integer between 0 and 10.",
    );
  }
  if (skipMineru && !pdfMarkdownPath.trim()) {
    return frontendError(
      "missing_pdf_markdown_path",
      "pdf_markdown_path is required when skip_mineru is true.",
    );
  }
  return null;
}

function frontendError(code: string, message: string): ApiError {
  return {
    status: 0,
    code,
    message,
    details: {},
  };
}

function TextField({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        disabled={disabled}
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
