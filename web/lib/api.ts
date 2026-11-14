import type { ApiErrorDetail } from "./types";

// The browser talks only to our backend. API keys live on the server and never reach this code.
export const API = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  detail: ApiErrorDetail;
  status: number;
  constructor(status: number, detail: ApiErrorDetail) {
    super(detail.message || detail.error);
    this.status = status;
    this.detail = detail;
  }
}

async function handle<T>(r: Response): Promise<T> {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = (body && body.detail) || body;
    throw new ApiError(
      r.status,
      typeof d === "object" && d ? (d as ApiErrorDetail) : { error: "http_error", message: String(d || r.statusText) },
    );
  }
  return body as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  return handle<T>(await fetch(`${API}${path}`, { cache: "no-store" }));
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return handle<T>(
    await fetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    }),
  );
}

export async function apiUpload<T>(path: string, form: FormData): Promise<T> {
  return handle<T>(await fetch(`${API}${path}`, { method: "POST", body: form }));
}
