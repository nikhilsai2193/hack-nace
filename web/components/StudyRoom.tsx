"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API, ApiError, apiGet, apiPost, apiUpload } from "@/lib/api";
import type { ApiErrorDetail, Citation, Health, InternEvent, Participant, Room, Source, StudyTurn } from "@/lib/types";
import { ErrorDetail } from "./ErrorDetail";

const COLORS: Record<string, string> = {
  human: "#8ab4f8", maya: "#f6aea9", leo: "#fdd663", sam: "#81c995", priya: "#c58af9", observer: "#d7aefb", group: "#9aa0a6",
};
const NAMES: Record<string, string> = { maya: "Maya", leo: "Leo", sam: "Sam", priya: "Priya", group: "Study group" };
const VOICE_PITCH: Record<string, number> = { maya: 1.1, leo: 1.3, sam: 0.8, priya: 1.0 };

type Tab = "chat" | "sources" | "events";

export default function StudyRoom({ roomId }: { roomId: string }) {
  const [room, setRoom] = useState<Room | null>(null);
  const [turns, setTurns] = useState<StudyTurn[]>([]);
  const [events, setEvents] = useState<InternEvent[]>([]);
  const [live, setLive] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<Tab>("chat");
  const [text, setText] = useState("");
  const [err, setErr] = useState<ApiErrorDetail | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [voice, setVoice] = useState(true);
  const [speaking, setSpeaking] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);
  const [exporting, setExporting] = useState(false);
  const loadedAt = useRef(Date.now());
  // Each queued turn starts fetching its Speechmatics audio at once, so clips are ready back to back.
  const speakQueue = useRef<{ turn: StudyTurn; audio: Promise<Blob | null> }[]>([]);
  const playing = useRef(false);
  const activeAudio = useRef<Set<HTMLAudioElement>>(new Set());
  const speakingNow = useRef<Set<string>>(new Set()); // turn ids being spoken right now
  const lastAsk = useRef<{ by: string; question: string } | null>(null);
  const tabId = useRef(Math.random().toString(36).slice(2));
  const claimDelay = useRef(Math.floor(Math.random() * 150)); // fixed per tab, so turn order is kept
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [caption, setCaption] = useState<StudyTurn | null>(null);
  const [captionsOn, setCaptionsOn] = useState(true);
  const voiceRef = useRef(voice);
  voiceRef.current = voice;
  const ttsRef = useRef(false); // Speechmatics TTS configured on the server
  const [speechAvailable, setSpeechAvailable] = useState(false); // Speechmatics STT configured
  const [ttsEngine, setTtsEngine] = useState<"speechmatics" | "browser">("browser");
  const recorder = useRef<MediaRecorder | null>(null);

  // ---------- speech: Speechmatics voices per persona, browser SpeechSynthesis as fallback; never blocks the app
  const fetchTts = (t: StudyTurn): Promise<Blob | null> => {
    if (!ttsRef.current) return Promise.resolve(null);
    return fetch(`${API}/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker: t.speaker, text: t.message }),
    })
      .then((r) => (r.ok ? r.blob() : null))
      .catch(() => null);
  };

  const browserSpeak = (t: StudyTurn, done: () => void) => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) { done(); return; }
    try {
      const u = new SpeechSynthesisUtterance(t.message.replace(/Answers?:[\s\S]*$/i, "").replace(/[*_#`]/g, "").slice(0, 600));
      u.pitch = VOICE_PITCH[t.speaker] ?? 1;
      u.rate = 1.05;
      u.onstart = () => setSpeaking(t.speaker);
      u.onend = u.onerror = () => done();
      window.speechSynthesis.speak(u);
    } catch { done(); }
  };

  // Meeting-style: a turn's text appears (caption + transcript) only once its speaker starts talking.
  const reveal = (id: string) => setRevealed((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));

  // A short, natural pause between speakers, so nobody sounds like they jump in.
  const GAP_MS = 500;

  const startNext = useCallback(async () => {
    const next = speakQueue.current.shift();
    if (!next) {
      playing.current = false;
      setSpeaking(null);
      setCaption(null);
      const asked = lastAsk.current;
      lastAsk.current = null;
      if (asked && !rec.current) setYourTurn(asked);
      return;
    }
    playing.current = true;
    const blob = await next.audio;
    if (!voiceRef.current) { reveal(next.turn.id); playing.current = false; return; }
    let advanced = false;
    const advance = () => {
      if (advanced) return;
      advanced = true;
      reveal(next.turn.id); // the line joins the transcript once it has been said
      speakingNow.current.delete(next.turn.id);
      startNext();
    };
    const begin = () => {
      if (next.turn.question_for_human) lastAsk.current = { by: next.turn.speaker, question: next.turn.question_for_human };
      speakingNow.current.add(next.turn.id);
      setSpeaking(next.turn.speaker);
      setCaption(next.turn);
    };
    if (blob) {
      const url = URL.createObjectURL(blob);
      const a = new Audio(url);
      activeAudio.current.add(a);
      const cleanup = () => { URL.revokeObjectURL(url); activeAudio.current.delete(a); };
      a.onplay = () => { begin(); setTtsEngine("speechmatics"); };
      a.onended = () => { cleanup(); setSpeaking(null); setTimeout(advance, GAP_MS); };
      const fallback = () => { cleanup(); begin(); browserSpeak(next.turn, () => setTimeout(advance, GAP_MS)); };
      a.onerror = fallback;
      a.play().catch(fallback);
    } else {
      setTtsEngine("browser");
      begin();
      browserSpeak(next.turn, () => setTimeout(advance, GAP_MS));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pump = useCallback(() => { if (!playing.current) startNext(); }, [startNext]);

  /** Stops every voice now (Voice off, or you start talking) and reveals what was still queued. */
  const stopSpeech = useCallback(() => {
    speakingNow.current.forEach((id) => reveal(id));
    speakingNow.current.clear();
    for (const q of speakQueue.current) reveal(q.turn.id);
    speakQueue.current = [];
    activeAudio.current.forEach((a) => a.pause());
    activeAudio.current.clear();
    playing.current = false;
    setSpeaking(null);
    setCaption(null);
    if (typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Only one open tab voices a turn, so two tabs on the same room never echo each other. */
  const claimTurn = (id: string): boolean => {
    if (typeof document !== "undefined" && document.visibilityState !== "visible") return false;
    try {
      const k = `studyroom-voice-${id}`;
      if (localStorage.getItem(k)) return false;
      localStorage.setItem(k, tabId.current);
      return localStorage.getItem(k) === tabId.current;
    } catch { return true; }
  };

  // ---------- SSE stream (replay, then live)
  useEffect(() => {
    apiGet<Health>("/health").then((h) => { setHealth(h); ttsRef.current = !!h.integrations.speechmatics; setSpeechAvailable(!!h.integrations.speechmatics); }).catch(() => {});
    const es = new EventSource(`${API}/sessions/${roomId}/events`);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.addEventListener("room", (m) => setRoom(JSON.parse((m as MessageEvent).data).data));
    es.addEventListener("turn", (m) => {
      const t: StudyTurn = JSON.parse((m as MessageEvent).data).data;
      setTurns((prev) => (prev.some((x) => x.id === t.id) ? prev : [...prev, t]));
      if (t.speaker !== "human") setLive("");
      const speakIt = t.speaker !== "human" && t.speaker !== "group" && t.created_at > loadedAt.current && voiceRef.current;
      if (!speakIt) { reveal(t.id); return; }
      setTimeout(() => {
        if (!claimTurn(t.id)) { reveal(t.id); return; } // another tab is voicing this turn
        speakQueue.current.push({ turn: t, audio: fetchTts(t) });
        pump();
      }, claimDelay.current);
    });
    es.addEventListener("intern_event", (m) => {
      const ev: InternEvent = JSON.parse((m as MessageEvent).data).data;
      setEvents((prev) => (prev.some((x) => x.id === ev.id) ? prev : [...prev.slice(-600), ev]));
    });
    es.addEventListener("live", (m) => {
      const d = JSON.parse((m as MessageEvent).data).data;
      if (d.kind === "text_delta") setLive((s) => (s + d.text).slice(-600));
    });
    return () => es.close();
  }, [roomId, pump]);

  useEffect(() => {
    if (!voice) stopSpeech();
  }, [voice, stopSpeech]);

  // ---------- actions
  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setErr(null);
    try { return await fn(); } catch (e) {
      setErr(e instanceof ApiError ? e.detail : { error: "network", message: String(e) });
    }
  }
  const send = () => {
    const t = text.trim();
    if (!t) return;
    setText("");
    stopSpeech();
    setYourTurn(null);
    run(() => apiPost(`/sessions/${roomId}/turns`, { text: t }));
  };
  const quiz = () => run(() => apiPost(`/sessions/${roomId}/quiz`, { topic: text.trim(), count: 3 }).then(() => setText("")));
  const summarize = () => run(() => apiPost(`/sessions/${roomId}/summarize`));
  const research = () => {
    const q = text.trim() || window.prompt("Concept to research (Keenable, labelled EXTERNAL):") || "";
    if (!q.trim()) return;
    setText("");
    run(() => apiPost(`/sessions/${roomId}/research`, { query: q }));
  };
  const [exportStart, setExportStart] = useState<number | null>(null);
  const [exportTick, setExportTick] = useState(0);
  useEffect(() => {
    if (!exportStart) return;
    const id = window.setInterval(() => setExportTick(Math.floor((Date.now() - exportStart) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [exportStart]);

  const exportAdobe = () => {
    // Open the tab NOW, inside the click: browsers block pop-ups opened minutes later after an await.
    const win = window.open("", "_blank");
    if (win) {
      win.document.write(`<!doctype html><title>Preparing study guide…</title><body style="margin:0;height:100vh;display:grid;place-items:center;background:#000;color:#e8eaed;font-family:system-ui,sans-serif"><div style="text-align:center"><div style="font-size:20px;margin-bottom:8px">Preparing your study guide…</div><div style="color:#9aa0a6">The study group is writing it, then Adobe turns it into a PDF.<br>This usually takes 2–3 minutes. This tab will show the PDF when it's ready.</div></div></body>`);
      win.document.close();
    }
    setExporting(true);
    setExportStart(Date.now());
    setExportTick(0);
    setNotice(null);
    run(async () => {
      try {
        const r = await apiPost<{ ok: boolean; error?: string; artifact?: { url: string }; recap_html?: string }>(
          `/sessions/${roomId}/export/adobe`);
        if (r.ok && r.artifact) {
          const url = `${API}${r.artifact.url}`;
          if (win && !win.closed) { win.location.href = url; setNotice("Study guide ready: it's open in the other tab."); }
          else setNotice("Study guide ready. Use “Open study guide” below (your browser blocked the new tab).");
        } else {
          if (win && !win.closed && r.recap_html) { win.document.open(); win.document.write(r.recap_html); win.document.close(); }
          else if (win && !win.closed) win.close();
          setNotice(`${r.error}.${r.recap_html ? " Showing the study guide as a web page instead." : ""}`);
        }
      } catch (e) {
        if (win && !win.closed) win.close();
        throw e;
      } finally {
        setExporting(false);
        setExportStart(null);
      }
    });
  };

  // ---------- talking: record → Speechmatics → sent automatically. Stops by itself after ~1.5 s of silence.
  const rec = useRef<{ mr: MediaRecorder; stream: MediaStream; ctx: AudioContext; timer: number; discard: boolean } | null>(null);
  const [level, setLevel] = useState(0);
  const [heard, setHeard] = useState(false);
  const [elapsed, setElapsed] = useState(0);

  const stopRecording = useCallback((discard = false) => {
    const r = rec.current;
    if (!r) return;
    r.discard = discard;
    window.clearInterval(r.timer);
    if (r.mr.state !== "inactive") r.mr.stop();
  }, []);

  const startRecording = useCallback(async () => {
    if (rec.current) return;
    setYourTurn(null);
    setErr(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (e) {
      setNotice(`Microphone unavailable: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    stopSpeech(); // you cut in: the group stops talking, like on a call
    const mr = new MediaRecorder(stream);
    const chunks: Blob[] = [];
    mr.ondataavailable = (ev) => chunks.push(ev.data);
    // Level meter only. Recording never stops by itself: it runs until you press Send (or Cancel).
    const ctx = new AudioContext();
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(an);
    const buf = new Float32Array(an.fftSize);
    const t0 = Date.now();
    let peak = 0.02;
    const timer = window.setInterval(() => {
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const rms = Math.sqrt(sum / buf.length);
      peak = Math.max(peak * 0.995, rms, 0.02); // auto-scale the meter to your voice
      setLevel(Math.min(1, rms / peak));
      if (rms > peak * 0.35) setHeard(true);
      setElapsed(Math.floor((Date.now() - t0) / 1000));
      if (Date.now() - t0 > 5 * 60 * 1000) stopRecording(false); // safety cap: 5 minutes
    }, 100);
    mr.onstop = async () => {
      const r = rec.current;
      rec.current = null;
      stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
      setRecording(false);
      setLevel(0);
      setHeard(false);
      setElapsed(0);
      if (!r || r.discard) { setNotice(null); return; } // Cancel
      setNotice("Transcribing…");
      const fd = new FormData();
      fd.append("file", new Blob(chunks, { type: mr.mimeType || "audio/webm" }), "recording.webm");
      const t = await run(() => apiUpload<{ text: string }>("/transcribe", fd));
      const said = t?.text.trim();
      if (!t) { setNotice(null); return; }
      if (!said) { setNotice("Didn't catch that. Nothing was sent; try again."); return; }
      setNotice(null);
      await run(() => apiPost(`/sessions/${roomId}/turns`, { text: said }));
    };
    rec.current = { mr, stream, ctx, timer, discard: false };
    mr.start();
    setRecording(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomId, stopRecording, stopSpeech]);

  const toggleRecord = () => (recording ? stopRecording(false) : startRecording());

  // ---------- "your turn": after the group finishes and someone asked you something
  const [yourTurn, setYourTurn] = useState<{ by: string; question: string } | null>(null);

  // ---------- derived
  const lastAiTurn = useMemo(() => [...turns].reverse().find((t) => t.speaker !== "human"), [turns]);
  const citedIds = useMemo(() => new Set((lastAiTurn?.citations ?? []).map((c) => c.source_id)), [lastAiTurn]);
  const latestBySpeaker = useMemo(() => {
    const m: Record<string, StudyTurn> = {};
    for (const t of turns) m[t.speaker] = t;
    return m;
  }, [turns]);

  if (!room) {
    return (
      <div style={{ padding: 40 }}>
        {connected ? "Loading room…" : "Connecting to the study room…"}
        {!connected && <p className="muted">If this persists the room may not exist (rooms live in server memory). <a href="/">Start a new room</a></p>}
      </div>
    );
  }

  const blocked = room.status === "blocked";
  const dead = blocked || room.status === "exited";
  const s2model = room.system2.replace(/^deepinfra\//, "");

  return (
    <div className="room">
      <div>
        <header className="topbar">
          <h1>{room.course ? `${room.course} · ` : ""}{room.title}</h1>
          <StatusPill room={room} connected={connected} />
          <span className="spacer" />
          <span className="pill"><span className={`dot ${blocked ? "err" : "ok"}`} />Nace Observer: {blocked ? "Blocked" : "Active"}</span>
          <span className="pill" title={room.system1}>System 1: Drex Max v1.1</span>
          <span className="pill" title={room.system2}>System 2: DeepInfra · {s2model}</span>
        </header>
        {room.status_detail && <div className={`banner ${dead ? "err" : "warn"}`}>{room.status_detail}</div>}
        {exporting && (
          <div className="banner info">
            Writing your study guide, then sending it to Adobe… {Math.floor(exportTick / 60)}:{String(exportTick % 60).padStart(2, "0")}
            <span className="muted"> (usually 2–3 minutes)</span>
          </div>
        )}
        {notice && <div className="banner warn" onClick={() => setNotice(null)}>{notice} <span className="muted">(click to dismiss)</span></div>}
      </div>

      <div className="main">
        <section className="stage">
          <div className="grid">
            {room.participants.map((p) =>
              p.kind === "observer" ? (
                <ObserverTile key={p.id} room={room} events={events} />
              ) : (
                <PersonTile key={p.id} p={p} sources={room.sources} speaking={speaking === p.id} busy={room.busy}
                  turn={caption?.speaker === p.id ? caption : undefined} recording={p.kind === "human" && recording} />
              ),
            )}
          </div>
          {(yourTurn || recording) && (
            <div className="turn-overlay">
              <div className="turn-card">
                {recording ? (
                  <>
                    <div className="turn-title">{heard ? "Listening…" : "Speak now"}</div>
                    <div className="turn-sub">Take your time. Press Send when you're done. {Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}</div>
                    <div className="meter"><div style={{ width: `${Math.round(level * 100)}%` }} /></div>
                    <div className="turn-actions">
                      <button className="btn primary" onClick={() => stopRecording(false)}>Send</button>
                      <button className="btn" onClick={() => stopRecording(true)}>Cancel</button>
                    </div>
                  </>
                ) : yourTurn && (
                  <>
                    <div className="turn-title">Your turn, {room.participants.find((p) => p.kind === "human")?.name || "you"}</div>
                    <div className="turn-q"><b style={{ color: COLORS[yourTurn.by] }}>{NAMES[yourTurn.by] || yourTurn.by}:</b> {yourTurn.question}</div>
                    <button className="mic-big" onClick={startRecording} disabled={!speechAvailable} aria-label="Answer by voice">
                      <svg viewBox="0 0 24 24" width="34" height="34" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>
                    </button>
                    <div className="turn-sub">
                      {!speechAvailable ? "Voice input needs Speechmatics. Type your answer below."
                        : "Tap the mic to answer, or type below."}
                    </div>
                    <div className="turn-actions">
                      <button className="btn" onClick={() => setYourTurn(null)}>Not now</button>
                    </div>
                  </>
                )}
              </div>
            </div>
          )}
          {captionsOn && caption && (
            <div className="caption">
              <span className="caption-who" style={{ color: COLORS[caption.speaker] }}>{NAMES[caption.speaker] || caption.speaker}</span>
              <span className="caption-text">{caption.message.replace(/Answers?:[\s\S]*$/i, "").replace(/[*_#`]/g, "")}</span>
            </div>
          )}
          {room.busy && !caption && (
            <div className="live">
              <b>Study group is thinking<span className="thinkdots" /></b>{" "}
              <span>{live ? "Writing the discussion…" : "Reading your sources; Drex is watching."}</span>
            </div>
          )}
          {err && <ErrorDetail detail={err} />}
        </section>

        <aside className="side">
          <div className="tabs">
            {(["chat", "sources", "events"] as Tab[]).map((t) => (
              <button key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
                {t === "chat" ? `Transcript (${turns.filter((x) => revealed.has(x.id)).length})` : t === "sources" ? `Sources (${room.sources.length})` : `Events (${events.filter((e) => e.severity !== "debug").length})`}
              </button>
            ))}
          </div>
          <div className="side-body">
            {tab === "chat" && <ChatPanel turns={turns.filter((t) => revealed.has(t.id))} sources={room.sources} />}
            {tab === "sources" && <SourcesPanel room={room} citedIds={citedIds} lastAiTurn={lastAiTurn} onError={setErr} />}
            {tab === "events" && <EventsPanel events={events} />}
          </div>
        </aside>
      </div>

      <footer className="controls">
        <input className="input" value={text} disabled={dead} placeholder="Ask the study group…"
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()} />
        <button className="btn primary" disabled={dead || !text.trim()} onClick={send}>Send</button>
        <button className="btn" disabled={dead} onClick={quiz} title="Uses the text box as the quiz topic, if any">Quiz me</button>
        <button className="btn" disabled={dead} onClick={summarize}>Summarize</button>
        <button className="btn" disabled={dead || !health?.integrations.keenable} onClick={research}
          title={health?.integrations.keenable ? "Keenable search, saved as an EXTERNAL source" : "Keenable not configured"}>
          Research concept
        </button>
        {room.artifacts.length > 0 && !exporting && (
          <span className="guide-pill">
            <a href={`${API}${room.artifacts[room.artifacts.length - 1].url}`} target="_blank" rel="noreferrer">Open study guide</a>
            <a href={`${API}${room.artifacts[room.artifacts.length - 1].url}?download=1`} title="Download PDF" aria-label="Download PDF">⤓</a>
          </span>
        )}
        <button className="btn" disabled={exporting} onClick={exportAdobe}
          title={health?.integrations.adobe ? "Final overview → Adobe PDF Services" : "Adobe not configured: shows the recap HTML"}>
          {exporting ? "Preparing study guide…" : room.artifacts.length ? "Export again" : "Export to Adobe"}
        </button>
        {health?.integrations.speechmatics && (
          <button className={`btn ${recording ? "danger" : ""}`} onClick={toggleRecord}>{recording ? "Send" : "Talk"}</button>
        )}
        <button className={`btn small ${captionsOn ? "" : "muted"}`} onClick={() => setCaptionsOn((c) => !c)} title="Live captions for whoever is speaking">
          CC {captionsOn ? "on" : "off"}
        </button>
        <button className="btn small" onClick={() => setVoice((v) => !v)}
          title={health?.integrations.speechmatics ? "AI voices by Speechmatics TTS (browser voice as fallback)" : "Browser speech synthesis for AI turns"}>
          {voice ? `Voice on · ${health?.integrations.speechmatics ? (ttsEngine === "browser" && speaking ? "browser" : "Speechmatics") : "browser"}` : "Voice off"}
        </button>
      </footer>
    </div>
  );
}

function StatusPill({ room, connected }: { room: Room; connected: boolean }) {
  const cls = room.status === "blocked" || room.status === "exited" || room.status === "error" ? "err"
    : room.status === "running" ? "warn" : "ok";
  const label = room.status === "running" ? "Discussing" : room.status === "ready" ? "Ready" : room.status.toUpperCase();
  return (
    <span className="pill"><span className={`dot ${cls}`} />{label}{!connected && <span className="err"> · stream reconnecting</span>}</span>
  );
}

function Wave() {
  return <span className="wave"><span /><span /><span /><span /></span>;
}

function CitationBadges({ citations, sources }: { citations: Citation[]; sources: Source[] }) {
  if (!citations?.length) return null;
  return (
    <div className="badges">
      {citations.map((c, i) => {
        const src = sources.find((s) => s.id === c.source_id);
        const ext = src?.origin === "external";
        const cls = ext ? "external" : c.app_check === "verified" ? "verified" : "unverified";
        const mark = c.app_check === "verified" ? "✓" : c.app_check === "unknown_source" ? "?" : "~";
        return (
          <span key={i} className={`badge ${cls}`}
            title={`${c.source_name || c.source_id} ${c.location}\n“${c.excerpt}”\nApp check (not Drex): ${c.app_check}`}>
            {mark} {c.source_id || "?"}{c.location ? ` · ${c.location}` : ""}{ext ? " · EXTERNAL" : ""}
          </span>
        );
      })}
    </div>
  );
}

function PersonTile({ p, turn, sources, speaking, busy, recording }: {
  p: Participant; turn?: StudyTurn; sources: Source[]; speaking: boolean; busy: boolean; recording: boolean;
}) {
  // Meeting tile: face, name, state. No text blocks; what is being said shows in the caption bar.
  const state = recording ? "speaking" : speaking ? "speaking" : p.state === "speaking" ? (busy ? "listening" : "idle") : p.state;
  return (
    <div className={`tile meet ${p.kind} ${state}`}>
      <div className="meet-center">
        <div className={`avatar big ${state === "speaking" ? "ring" : ""}`} style={{ background: COLORS[p.id] || "#9aa0a6" }}>{p.initials}</div>
      </div>
      <div className="meet-foot">
        <span className="tile-name">{p.name}</span>
        {state === "speaking" && <Wave />}
        <span className="tile-role">{recording ? "speaking…" : p.role}</span>
        <span className="tile-state">{state === "thinking" ? <span>thinking<span className="thinkdots" /></span> : state === "speaking" ? "" : state}</span>
      </div>
      {turn && <div className="meet-badges"><CitationBadges citations={turn.citations} sources={sources} /></div>}
    </div>
  );
}

function ObserverTile({ room, events }: { room: Room; events: InternEvent[] }) {
  // Only genuine System 1 events that came from Intern; nothing inferred by the app.
  const real = events.filter((e) => e.layer === "system1" &&
    ["decision", "tool_guard", "nudge", "process_check", "run_end"].includes(e.raw_type ?? ""));
  const count = (k: string) => events.filter((e) => e.kind === k).length;
  const tools = events.filter((e) => e.layer === "tool");
  const lastTool = tools[tools.length - 1];
  const lastRaw = [...real].reverse().find((e) => e.raw_type === "decision" || e.raw_type === "tool_guard" || e.raw_type === "nudge");
  const blocked = room.status === "blocked";
  return (
    <div className={`tile observer ${blocked ? "blocked" : ""}`}>
      <div className="tile-head">
        <div className="avatar" style={{ background: COLORS.observer }}>N1</div>
        <div>
          <div className="tile-name">Nace Observer</div>
          <div className="tile-role">Nace Drex Max v1.1 — System 1 Supervisor</div>
        </div>
        <div className="tile-state">{blocked ? "blocked" : room.busy ? "watching" : "listening"}</div>
      </div>
      {real.length === 0 ? (
        <div className="tile-msg empty">Waiting for Intern System 1 events.</div>
      ) : (
        <div className="tile-msg">
          <div className="obs-row"><b>Status:</b> {room.drex_status}</div>
          {room.latest_decision && (
            <div className="obs-row">
              <b>Latest:</b> <span className={room.latest_decision.severity === "error" ? "err" : room.latest_decision.severity === "warning" ? "warn" : ""}>
                {room.latest_decision.title}</span>
              {room.latest_decision.detail && <div className="muted">{room.latest_decision.detail}</div>}
            </div>
          )}
          {lastTool && <div className="obs-row"><b>Tool:</b> <span className="muted">{lastTool.title}</span></div>}
          <div className="obs-stats">
            <span className="pill">decisions {count("system1_decision")}</span>
            <span className="pill warn">warnings {count("system1_warning")}</span>
            <span className="pill warn">nudges {count("system1_nudge")}</span>
            <span className="pill err">blocked {count("system1_blocked")}</span>
          </div>
          {lastRaw && (
            <details style={{ marginTop: 8 }}>
              <summary>raw {lastRaw.raw_type} JSON</summary>
              <pre>{JSON.stringify(lastRaw.raw_payload, null, 2)}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

/** Hides a quiz's answer behind a toggle so the transcript doesn't give it away. */
function ChatText({ message }: { message: string }) {
  const m = message.match(/^([\s\S]*?)\s*\(?\b(Answers?:[\s\S]*)$/i);
  if (!m) return <div className="chat-text">{message}</div>;
  return (
    <div className="chat-text">
      {m[1]}
      <details><summary>Show answer</summary><div className="chat-text">{m[2]}</div></details>
    </div>
  );
}

function ChatPanel({ turns, sources }: { turns: StudyTurn[]; sources: Source[] }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Braces matter: scrollIntoView can return a Promise in newer browsers, and an effect may only return a cleanup.
    end.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns.length]);
  if (!turns.length) return <p className="muted">No messages yet. Add a source, then ask the group a question.</p>;
  return (
    <div>
      {turns.map((t) => (
        <div key={t.id} className="chat-item">
          <div className="chat-who" style={{ color: COLORS[t.speaker] || COLORS.group }}>
            {t.speaker === "human" ? "You" : NAMES[t.speaker] || t.speaker}
            {t.mode !== "discussion" && <span className="muted"> · {t.mode}</span>}
            {t.speaker === "group" && <span className="muted"> · unattributed (no JSON in reply)</span>}
          </div>
          <ChatText message={t.message} />
          {t.question_for_human && <div className="chat-q">→ {t.question_for_human}</div>}
          <CitationBadges citations={t.citations} sources={sources} />
        </div>
      ))}
      <div ref={end} />
    </div>
  );
}

function SourcesPanel({ room, citedIds, lastAiTurn, onError }: {
  room: Room; citedIds: Set<string>; lastAiTurn?: StudyTurn; onError: (d: ApiErrorDetail) => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState("notes");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function addText() {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await apiPost(`/sessions/${room.id}/sources`, { name: name || "Pasted text", kind, text });
      setText(""); setName("");
    } catch (e) { if (e instanceof ApiError) onError(e.detail); } finally { setBusy(false); }
  }
  async function upload(f: File) {
    setBusy(true);
    const fd = new FormData();
    fd.append("file", f);
    fd.append("name", name || f.name);
    const ext = f.name.split(".").pop()?.toLowerCase();
    fd.append("kind", kind === "notes" && ext === "pdf" ? "pdf" : kind === "notes" && ext === "pptx" ? "slides" : kind);
    try { await apiUpload(`/sessions/${room.id}/sources`, fd); setName(""); }
    catch (e) { if (e instanceof ApiError) onError(e.detail); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  }
  const [yt, setYt] = useState("");
  async function addYoutube() {
    if (!yt.trim()) return;
    setBusy(true);
    try { await apiPost(`/sessions/${room.id}/sources/youtube`, { url: yt.trim() }); setYt(""); }
    catch (e) { if (e instanceof ApiError) onError(e.detail); }
    finally { setBusy(false); }
  }
  const sorted = [...room.sources].sort((a, b) => a.priority - b.priority || a.id.localeCompare(b.id));
  return (
    <div>
      <div className="src">
        <b>Add a source</b>
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <input className="input" placeholder="Name (e.g. Lecture 4 slides)" value={name} onChange={(e) => setName(e.target.value)} />
          <select className="select" style={{ width: 150 }} value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="notes">Notes</option>
            <option value="handwritten">Handwritten</option>
            <option value="slides">Slides</option>
            <option value="pdf">PDF</option>
            <option value="transcript">Transcript</option>
          </select>
        </div>
        <textarea className="textarea" style={{ marginTop: 6 }} placeholder="Paste notes or a lecture/YouTube transcript…"
          value={text} onChange={(e) => setText(e.target.value)} />
        <div style={{ display: "flex", gap: 6, marginTop: 6, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn small primary" disabled={busy || !text.trim()} onClick={addText}>Add text</button>
          <button className="btn small" disabled={busy} onClick={() => fileRef.current?.click()}>Upload PDF/PPTX/TXT{room.ocr_available ? "/PNG/JPG" : ""}</button>
          <input ref={fileRef} type="file" hidden accept={`.pdf,.pptx,.txt,.md,.markdown${room.ocr_available ? ",.png,.jpg,.jpeg" : ""}`}
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          {!room.ocr_available && <span className="warn" style={{ fontSize: 12 }}>OCR unavailable — images can't be read</span>}
        </div>
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <input className="input" placeholder="YouTube lecture link (uses its captions)" value={yt}
            onChange={(e) => setYt(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addYoutube()} />
          <button className="btn small" disabled={busy || !yt.trim()} onClick={addYoutube}>Add video</button>
          {busy && <span className="muted">working…</span>}
        </div>
      </div>
      {!sorted.length && <p className="muted">No sources yet. Pasted text is the most reliable path.</p>}
      {sorted.map((s) => (
        <div key={s.id} className={`src ${citedIds.has(s.id) ? "cited" : ""}`}>
          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <b>{s.id}</b> {s.name}
            <span className={`badge ${s.origin === "external" ? "external" : ""}`}>{s.origin === "external" ? "EXTERNAL" : "COURSE"}</span>
            <span className={`badge ${s.readiness === "ready" ? "verified" : "unverified"}`}>{s.readiness === "ocr_unavailable" ? "OCR unavailable" : s.readiness}</span>
            {citedIds.has(s.id) && <span className="badge">cited this turn</span>}
          </div>
          <div className="ev-meta">{s.priority_label} · {s.kind} · {s.chars.toLocaleString()} chars · {s.file_path}</div>
          {s.readiness_detail && <div className="ev-meta warn">{s.readiness_detail}</div>}
          {s.excerpt && <div className="src-excerpt">{s.excerpt}</div>}
          {lastAiTurn?.citations.filter((c) => c.source_id === s.id).map((c, i) => (
            <div key={i} className="ev-detail" style={{ marginTop: 6 }}>
              <span className={c.app_check === "verified" ? "ok" : "warn"}>{c.app_check === "verified" ? "✓ verified" : "~ " + c.app_check}</span>
              {" "}{c.location} — “{c.excerpt}”
            </div>
          ))}
        </div>
      ))}
      <p className="muted" style={{ fontSize: 11 }}>Citation check is the app's own (source id exists and the excerpt is in the source text), not Drex's.</p>
    </div>
  );
}

const FILTERS = { all: "All", system1: "Drex / System 1", tool: "Tools", system2: "System 2", ui: "App" } as const;

function EventsPanel({ events }: { events: InternEvent[] }) {
  const [filter, setFilter] = useState<keyof typeof FILTERS>("all");
  const [debug, setDebug] = useState(false);
  const shown = events.filter((e) => (debug || e.severity !== "debug") && (filter === "all" || e.layer === filter));
  return (
    <div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 10 }}>
        {(Object.keys(FILTERS) as (keyof typeof FILTERS)[]).map((f) => (
          <button key={f} className={`btn small ${filter === f ? "primary" : ""}`} onClick={() => setFilter(f)}>{FILTERS[f]}</button>
        ))}
        <label className="muted" style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={debug} onChange={(e) => setDebug(e.target.checked)} /> debug
        </label>
      </div>
      {!shown.length && <p className="muted">No events yet.</p>}
      {[...shown].reverse().map((e) => (
        <div key={e.id} className={`ev ${e.layer} ${e.severity}`}>
          <div className="ev-title">{e.title}</div>
          <div className="ev-meta">{new Date(e.created_at).toLocaleTimeString()} · {e.kind}{e.raw_type ? ` · intern:${e.raw_type}` : " · app"}</div>
          {e.detail && <div className="ev-detail">{e.detail.length > 400 ? e.detail.slice(0, 400) + "…" : e.detail}</div>}
          {e.raw_payload != null && (
            <details><summary>raw</summary><pre>{JSON.stringify(e.raw_payload, null, 2)}</pre></details>
          )}
        </div>
      ))}
    </div>
  );
}
