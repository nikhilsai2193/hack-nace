import type { ApiErrorDetail } from "@/lib/types";

/** Shows a backend error, with Intern's own stderr word for word when there is one. */
export function ErrorDetail({ detail }: { detail: ApiErrorDetail }) {
  const { error, message, intern_stderr, ...rest } = detail;
  const extra = Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== undefined && v !== null));
  return (
    <div className="errorbox">
      <div className="err"><b>{message}</b> <span className="muted">({error})</span></div>
      {intern_stderr && (
        <>
          <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>Intern stderr (verbatim):</div>
          <pre>{intern_stderr}</pre>
        </>
      )}
      {Object.keys(extra).length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary>details</summary>
          <pre>{JSON.stringify(extra, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
