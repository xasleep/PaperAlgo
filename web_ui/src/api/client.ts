import type {
  ApiError,
  ArtifactSummary,
  CancelResponse,
  JobCreateResponse,
  JobDetail,
  JobListItem,
  LogsResponse,
  RepoFileResponse,
  RepoTreeResponse,
  SettingsStatus,
  WebSettingsPayload,
} from "./types";

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL;
const API_BASE_URL =
  configuredApiBaseUrl !== undefined
    ? configuredApiBaseUrl.replace(/\/+$/, "")
    : import.meta.env.PROD
      ? ""
      : "http://localhost:8000";

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

type ErrorEnvelope = {
  error?: {
    code?: unknown;
    message?: unknown;
    details?: unknown;
  };
};

export class ApiClientError extends Error {
  readonly apiError: ApiError;

  constructor(apiError: ApiError) {
    super(apiError.message);
    this.name = "ApiClientError";
    this.apiError = apiError;
  }
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiClientError) {
    return error.apiError;
  }
  if (error instanceof Error) {
    return {
      status: 0,
      code: "network_error",
      message: error.message || "Network request failed.",
      details: {},
    };
  }
  return {
    status: 0,
    code: "unknown_error",
    message: "Unexpected frontend error.",
    details: {},
  };
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (response.ok) {
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  let envelope: ErrorEnvelope | null = null;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    envelope = null;
  }

  const details = envelope?.error?.details;
  const apiError: ApiError = {
    status: response.status,
    code:
      typeof envelope?.error?.code === "string"
        ? envelope.error.code
        : `http_${response.status}`,
    message:
      typeof envelope?.error?.message === "string"
        ? envelope.error.message
        : response.statusText || "Request failed.",
    details:
      details && typeof details === "object" && !Array.isArray(details)
        ? (details as Record<string, unknown>)
        : {},
  };
  throw new ApiClientError(apiError);
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    const response = await fetch(apiUrl(path), {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...init?.headers,
      },
    });
    return await parseResponse<T>(response);
  } catch (error) {
    if (error instanceof ApiClientError) {
      throw error;
    }
    throw new ApiClientError(toApiError(error));
  }
}

export const api = {
  health: () => requestJson<{ status: string }>("/health"),

  getSettingsStatus: () => requestJson<SettingsStatus>("/settings/status"),

  saveSettings: (payload: WebSettingsPayload) =>
    requestJson<SettingsStatus>("/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listJobs: () => requestJson<{ jobs: JobListItem[] }>("/jobs"),

  createJob: (formData: FormData) =>
    requestJson<JobCreateResponse>("/jobs", {
      method: "POST",
      body: formData,
    }),

  getJob: (jobId: string) => requestJson<JobDetail>(`/jobs/${encodeURIComponent(jobId)}`),

  cancelJob: (jobId: string) =>
    requestJson<CancelResponse>(`/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    }),

  getArtifacts: (jobId: string) =>
    requestJson<ArtifactSummary>(`/jobs/${encodeURIComponent(jobId)}/artifacts`),

  getLogs: (jobId: string, file?: string) => {
    const params = new URLSearchParams();
    if (file) {
      params.set("file", file);
    }
    const query = params.toString();
    return requestJson<LogsResponse>(
      `/jobs/${encodeURIComponent(jobId)}/logs${query ? `?${query}` : ""}`,
    );
  },

  getRepoTree: (jobId: string) =>
    requestJson<RepoTreeResponse>(`/jobs/${encodeURIComponent(jobId)}/repo/tree`),

  getRepoFile: (jobId: string, path: string) => {
    const params = new URLSearchParams({ path });
    return requestJson<RepoFileResponse>(
      `/jobs/${encodeURIComponent(jobId)}/repo/file?${params.toString()}`,
    );
  },

  downloadRepo: async (jobId: string): Promise<Blob> => {
    try {
      const response = await fetch(apiUrl(`/jobs/${encodeURIComponent(jobId)}/repo/download`));
      if (!response.ok) {
        await parseResponse<never>(response);
      }
      return await response.blob();
    } catch (error) {
      if (error instanceof ApiClientError) {
        throw error;
      }
      throw new ApiClientError(toApiError(error));
    }
  },
};

export { API_BASE_URL };
