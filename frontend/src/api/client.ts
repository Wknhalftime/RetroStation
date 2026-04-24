const API_BASE = "http://127.0.0.1:8000";
const TOKEN = "dev-token";

// ---------------------------------------------------------------------------
// Error hierarchy
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class AuthError extends ApiError {
  constructor(message = "Unauthorized") {
    super(401, message);
    this.name = "AuthError";
  }
}

export class ConflictError extends ApiError {
  constructor(message = "Conflict") {
    super(409, message);
    this.name = "ConflictError";
  }
}

export class ValidationError extends ApiError {
  constructor(public readonly detail: { loc: unknown[]; msg: string; type: string }[]) {
    super(422, "Validation error");
    this.name = "ValidationError";
  }
}

export class ServerError extends ApiError {
  constructor(status: number, message = "Internal server error") {
    super(status, message);
    this.name = "ServerError";
  }
}

export class EmptyResponseError extends ApiError {
  constructor(status: number) {
    super(status, `Expected JSON body but received empty response (${status})`);
    this.name = "EmptyResponseError";
  }
}

// ---------------------------------------------------------------------------
// Error factory
// ---------------------------------------------------------------------------

async function throwForStatus(res: Response): Promise<never> {
  if (res.status === 401) throw new AuthError();
  if (res.status === 409) {
    const body = await res.json().catch(() => ({}));
    throw new ConflictError((body as { detail?: string }).detail ?? "Conflict");
  }
  if (res.status === 422) {
    const body = await res.json().catch(() => ({ detail: [] }));
    throw new ValidationError(
      (body as { detail: { loc: unknown[]; msg: string; type: string }[] }).detail ?? []
    );
  }
  if (res.status >= 500) {
    const body = await res.json().catch(() => ({}));
    throw new ServerError(
      res.status,
      (body as { detail?: string }).detail ?? `Server error ${res.status}`
    );
  }
  const body = await res.json().catch(() => ({}));
  throw new ApiError(res.status, (body as { detail?: string }).detail ?? `HTTP ${res.status}`);
}

// ---------------------------------------------------------------------------
// apiFetch — JSON requests
// ---------------------------------------------------------------------------

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Airwave-Token": TOKEN,
      ...(options.headers ?? {}),
    },
  });

  if (!res.ok) await throwForStatus(res);
  return parseJsonBody<T>(res);
}

// Application-level contract: the RetroStation backend always returns a JSON
// body for 2xx responses except 204 No Content and 205 Reset Content (which
// have no body by HTTP spec). An empty or whitespace-only body on any other
// 2xx is a backend/proxy regression and must surface — silently casting
// undefined to T would launder the bug into a TypeError far from the call.
async function parseJsonBody<T>(res: Response): Promise<T> {
  if (res.status === 204 || res.status === 205) {
    return undefined as T;
  }
  const text = await res.text();
  if (!text.trim()) {
    throw new EmptyResponseError(res.status);
  }
  return JSON.parse(text) as T;
}

// ---------------------------------------------------------------------------
// apiUpload — multipart/form-data (no Content-Type — browser sets boundary)
// ---------------------------------------------------------------------------

export async function apiUpload<T>(path: string, formData: FormData): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "X-Airwave-Token": TOKEN },
    body: formData,
  });

  if (!res.ok) await throwForStatus(res);
  return parseJsonBody<T>(res);
}

// ---------------------------------------------------------------------------
// apiDownload — POST that returns a Blob
// ---------------------------------------------------------------------------

export async function apiDownload(path: string, body?: unknown): Promise<Blob> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Airwave-Token": TOKEN,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) await throwForStatus(res);
  return res.blob();
}
