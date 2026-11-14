export type ParticipantState = "idle" | "listening" | "thinking" | "speaking" | "blocked";

export interface Citation {
  source_id: string;
  source_name: string;
  location: string;
  excerpt: string;
  app_check: "verified" | "source_found_excerpt_missing" | "unknown_source";
}

export interface Participant {
  id: string;
  name: string;
  role: string;
  kind: "human" | "ai" | "observer";
  initials: string;
  state: ParticipantState;
  latest_message: string | null;
  latest_citations: Citation[];
}

export interface Source {
  id: string;
  name: string;
  kind: string;
  priority: number;
  priority_label: string;
  origin: "course" | "external";
  file_path: string;
  extra_files: string[];
  readiness: "ready" | "extracting" | "failed" | "ocr_unavailable";
  readiness_detail: string | null;
  chars: number;
  excerpt: string;
}

export interface StudyTurn {
  id: string;
  speaker: string;
  message: string;
  citations: Citation[];
  question_for_human: string | null;
  next_action: string | null;
  mode: string;
  created_at: number;
}

export interface InternEvent {
  id: string;
  kind: string;
  layer: "system1" | "system2" | "tool" | "ui";
  severity: "debug" | "info" | "success" | "warning" | "error";
  title: string;
  detail: string | null;
  raw_type: string | null;
  raw_payload: unknown;
  created_at: number;
}

export interface Room {
  id: string;
  title: string;
  course: string;
  status: "starting" | "ready" | "running" | "blocked" | "error" | "exited";
  status_detail: string | null;
  system1: string;
  system2: string;
  intern_version: string | null;
  intern_args: string[];
  busy: boolean;
  current_mode: string;
  participants: Participant[];
  sources: Source[];
  drex_status: string;
  latest_decision: { title: string; detail: string | null; kind: string; severity: string; at: number } | null;
  artifacts: { name: string; url: string; bytes: number }[];
  ocr_available: boolean;
}

export interface Health {
  ok: boolean;
  intern: { found: boolean; version: string | null; install: string | null; ocr_extra: boolean };
  system1: string;
  system2: string;
  keys: Record<string, boolean>;
  integrations: { keenable: boolean; adobe: boolean; speechmatics: boolean; speechmatics_tts?: boolean };
}

export interface ApiErrorDetail {
  error: string;
  message: string;
  intern_stderr?: string;
  [k: string]: unknown;
}
