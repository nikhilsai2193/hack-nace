"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { API, ApiError, apiGet, apiPost, apiUpload } from "@/lib/api";
import type { ApiErrorDetail, Health, Room, Source } from "@/lib/types";
import { ErrorDetail } from "@/components/ErrorDetail";

type Pending = { id: string; file: File; kind: string };
type Step = { label: string; state: "pending" | "running" | "ok" | "error"; detail?: string };

const ACCEPT = ".pdf,.pptx,.txt,.md,.markdown";
const ACCEPT_OCR = ACCEPT + ",.png,.jpg,.jpeg";

function guessKind(f: File): string {
  const ext = f.name.split(".").pop()?.toLowerCase() || "";
  if (ext === "pptx") return "slides";
  if (ext === "pdf") return "pdf";
  if (["png", "jpg", "jpeg"].includes(ext)) return "handwritten";
  return "notes";
}

export default function Landing() {
  const router = useRouter();
  const [health, setHealth] = useState<Health | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);
  const [course, setCourse] = useState("");
  const [title, setTitle] = useState("Midterm study session");
  const [name, setName] = useState("You");
  const [files, setFiles] = useState<Pending[]>([]);
  const [links, setLinks] = useState<string[]>([]);
  const [linkInput, setLinkInput] = useState("");
  const [notesName, setNotesName] = useState("My notes");
  const [notesKind, setNotesKind] = useState("notes");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiErrorDetail | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [roomId, setRoomId] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiGet<Health>("/health")
      .then(setHealth)
      .catch((e) => setHealthErr(`Backend not reachable at ${API}: ${e.message}`));
  }, []);

  const ocr = !!health?.intern.ocr_extra;
  const addFiles = (list: FileList | null) => {
    if (!list) return;
    const ok = Array.from(list).filter((f) => (ocr ? ACCEPT_OCR : ACCEPT).split(",").some((ext) => f.name.toLowerCase().endsWith(ext)));
    setFiles((prev) => [...prev, ...ok.map((f) => ({ id: `${f.name}-${f.size}-${Math.random()}`, file: f, kind: guessKind(f) }))]);
    if (ok.length < list.length) setErr({ error: "unsupported_type", message: `Skipped unsupported files. Use PDF, PPTX, TXT or MD${ocr ? ", PNG or JPG" : ""}. For .ppt or Keynote, export as PDF or PPTX.` });
  };
  const addLinks = () => {
    const found = linkInput.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
    const valid = found.filter((u) => /youtu\.?be|^[A-Za-z0-9_-]{11}$/.test(u));
    if (valid.length) setLinks((prev) => Array.from(new Set([...prev, ...valid])));
    if (valid.length < found.length) setErr({ error: "bad_youtube_url", message: "Some links weren't YouTube video links and were skipped." });
    setLinkInput("");
  };

  async function create() {
    setBusy(true);
    setErr(null);
    const pendingLinks = linkInput.trim() ? [...links, ...linkInput.split(/[\s,]+/).filter(Boolean)] : links;
    const plan: Step[] = [
      { label: "Start the study room (Intern + Drex)", state: "running" },
      ...files.map((f) => ({ label: `Upload ${f.file.name}`, state: "pending" as const })),
      ...pendingLinks.map((l) => ({ label: `YouTube captions: ${l}`, state: "pending" as const })),
      ...(notes.trim() ? [{ label: `Add pasted text: ${notesName || "Pasted notes"}`, state: "pending" as const }] : []),
    ];
    setSteps(plan);
    const set = (i: number, patch: Partial<Step>) => setSteps((prev) => prev.map((s, j) => (j === i ? { ...s, ...patch } : s)));
    let room: Room;
    try {
      room = (await apiPost<{ room: Room }>("/sessions", { title, course, human_name: name })).room;
      set(0, { state: "ok" });
      setRoomId(room.id);
    } catch (e) {
      set(0, { state: "error" });
      setErr(e instanceof ApiError ? e.detail : { error: "network", message: String(e) });
      setBusy(false);
      return;
    }
    let i = 1, failed = 0;
    const describe = (src: Source) =>
      src.readiness === "ready" ? (src.readiness_detail || `${src.chars.toLocaleString()} characters`) : (src.readiness_detail || src.readiness);
    for (const f of files) {
      set(i, { state: "running" });
      const fd = new FormData();
      fd.append("file", f.file);
      fd.append("name", f.file.name.replace(/\.[^.]+$/, ""));
      fd.append("kind", f.kind);
      try {
        const src = await apiUpload<Source>(`/sessions/${room.id}/sources`, fd);
        const ok = src.readiness === "ready";
        if (!ok) failed++;
        set(i, { state: ok ? "ok" : "error", detail: describe(src) });
      } catch (e) {
        failed++;
        set(i, { state: "error", detail: e instanceof ApiError ? e.detail.message : String(e) });
      }
      i++;
    }
    for (const l of pendingLinks) {
      set(i, { state: "running" });
      try {
        const src = await apiPost<Source>(`/sessions/${room.id}/sources/youtube`, { url: l });
        set(i, { state: "ok", detail: `${src.name} · ${describe(src)}` });
      } catch (e) {
        failed++;
        set(i, { state: "error", detail: e instanceof ApiError ? e.detail.message : String(e) });
      }
      i++;
    }
    if (notes.trim()) {
      set(i, { state: "running" });
      try {
        await apiPost(`/sessions/${room.id}/sources`, { name: notesName || "Pasted notes", kind: notesKind, text: notes });
        set(i, { state: "ok" });
      } catch (e) {
        failed++;
        set(i, { state: "error", detail: e instanceof ApiError ? e.detail.message : String(e) });
      }
    }
    if (failed === 0) router.push(`/room/${room.id}`);
    else setBusy(false); // stay so the errors are readable; "Open room" is offered below
  }

  const key = (k: string) =>
    health ? <span className={health.keys[k] ? "ok" : "err"}>{health.keys[k] ? "set" : "missing"}</span> : "…";

  return (
    <main className="landing">
      <section className="card hero">
        <h1>Study Room</h1>
        <p>
          Study with Maya, Leo, Sam and Priya, four AI study partners grounded in your own notes, slides, PDFs and
          lecture transcripts. <b>DeepInfra</b> writes the conversation. <b>Nace Drex Max v1.1</b> supervises every
          turn and tool call through the <a href="https://github.com/nace-ai/intern">Intern</a> harness.
        </p>
        <label className="label">Course</label>
        <input className="input" value={course} onChange={(e) => setCourse(e.target.value)} placeholder="e.g. CS 161 — Operating Systems" />
        <label className="label">Room title</label>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
        <label className="label">Your name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        <label className="label">Course materials: slides, PDFs, notes</label>
        <div className={`drop ${drag ? "over" : ""}`} onClick={() => fileRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer.files); }}>
          <div className="drop-title">Drop files here or click to upload</div>
          <div className="muted" style={{ fontSize: 12 }}>PDF, PowerPoint (.pptx), TXT, MD{ocr ? ", photos of handwritten notes (PNG/JPG)" : ""}</div>
          <input ref={fileRef} type="file" multiple hidden accept={ocr ? ACCEPT_OCR : ACCEPT}
            onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
        </div>
        {files.length > 0 && (
          <div className="file-list">
            {files.map((f) => (
              <div key={f.id} className="file-row">
                <span className="file-ext">{f.file.name.split(".").pop()?.toUpperCase()}</span>
                <span className="file-name" title={f.file.name}>{f.file.name}</span>
                <span className="muted" style={{ fontSize: 12 }}>{Math.max(1, Math.round(f.file.size / 1024))} KB</span>
                <select className="select small" value={f.kind} onChange={(e) => setFiles((prev) => prev.map((x) => (x.id === f.id ? { ...x, kind: e.target.value } : x)))}>
                  <option value="slides">Slides</option>
                  <option value="pdf">PDF / reading</option>
                  <option value="handwritten">Handwritten notes</option>
                  <option value="notes">Notes</option>
                  <option value="transcript">Transcript</option>
                </select>
                <button className="x" aria-label={`Remove ${f.file.name}`} onClick={() => setFiles((prev) => prev.filter((x) => x.id !== f.id))}>×</button>
              </div>
            ))}
          </div>
        )}

        <label className="label">YouTube lecture links</label>
        <div style={{ display: "flex", gap: 8 }}>
          <input className="input" value={linkInput} onChange={(e) => setLinkInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addLinks(); } }}
            placeholder="https://www.youtube.com/watch?v=…" />
          <button className="btn" onClick={addLinks} disabled={!linkInput.trim()}>Add</button>
        </div>
        {links.length > 0 && (
          <div className="chips">
            {links.map((l) => (
              <span key={l} className="chip">▶ {l.replace(/^https?:\/\/(www\.)?/, "").slice(0, 48)}
                <button className="x" aria-label="Remove link" onClick={() => setLinks((prev) => prev.filter((x) => x !== l))}>×</button>
              </span>
            ))}
          </div>
        )}
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>Uses the video&apos;s own captions. If it has none, paste the transcript below.</div>

        <details className="paste" open={!!notes}>
          <summary>Paste notes or a transcript instead</summary>
          <div style={{ display: "flex", gap: 8, margin: "8px 0 6px" }}>
            <input className="input" value={notesName} onChange={(e) => setNotesName(e.target.value)} placeholder="Source name" />
            <select className="select" style={{ width: 180 }} value={notesKind} onChange={(e) => setNotesKind(e.target.value)}>
              <option value="notes">Notes</option>
              <option value="handwritten">Handwritten notes (typed)</option>
              <option value="slides">Slides text</option>
              <option value="transcript">Lecture transcript</option>
            </select>
          </div>
          <textarea className="textarea" style={{ minHeight: 110 }} value={notes} onChange={(e) => setNotes(e.target.value)}
            placeholder="Paste notes, slide text, or a transcript…" />
        </details>

        <div style={{ marginTop: 14, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn primary" disabled={busy || !health?.intern.found} onClick={create}>
            {busy ? "Setting up your room…" : "Create study room"}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            {files.length + links.length + (notes.trim() ? 1 : 0)} source(s) ready
          </span>
        </div>
        {steps.length > 0 && (
          <div className="steps">
            {steps.map((st, k) => (
              <div key={k} className={`step ${st.state}`}>
                <span className="step-icon">{st.state === "ok" ? "✓" : st.state === "error" ? "✗" : st.state === "running" ? "…" : "•"}</span>
                <span>{st.label}{st.detail && <span className="muted"> · {st.detail}</span>}</span>
              </div>
            ))}
            {!busy && roomId && steps.some((st) => st.state === "error") && (
              <button className="btn primary small" style={{ marginTop: 8 }} onClick={() => router.push(`/room/${roomId}`)}>Open the room anyway</button>
            )}
          </div>
        )}
        {err && <ErrorDetail detail={err} />}
      </section>

      <aside className="card">
        <h3 style={{ marginTop: 0 }}>Backend status</h3>
        {healthErr && <div className="errorbox">{healthErr}</div>}
        {health && (
          <>
            <div className="kv"><span>Intern</span>
              <span className={health.intern.found ? "ok" : "err"}>{health.intern.found ? health.intern.version : "not found"}</span></div>
            {!health.intern.found && <pre className="muted">{health.intern.install}</pre>}
            <div className="kv"><span>System 1</span><span>{health.system1}</span></div>
            <div className="kv"><span>System 2</span><span style={{ textAlign: "right" }}>{health.system2}</span></div>
            <div className="kv"><span>NACE_API_KEY</span>{key("NACE_API_KEY")}</div>
            <div className="kv"><span>DEEPINFRA_API_KEY</span>{key("DEEPINFRA_API_KEY")}</div>
            <div className="kv"><span>OCR (Intern [ocr] extra)</span>
              <span className={health.intern.ocr_extra ? "ok" : "warn"}>{health.intern.ocr_extra ? "available" : "OCR unavailable"}</span></div>
            <div className="kv"><span>Adobe PDF Services</span>{key("ADOBE_PDF_SERVICES")}</div>
            <div className="kv"><span>Speechmatics</span>{key("SPEECHMATICS_API_KEY")}</div>
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              Keys stay on the server; the browser only sees whether each one is set.
            </p>
          </>
        )}
      </aside>
    </main>
  );
}
