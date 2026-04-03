# Phase 4 — Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete React frontend for RetroStation — a radio playlist reconstruction curator tool — so every API endpoint from Phase 3 has a working UI.

**Architecture:** React 19 SPA with TanStack Query for data fetching, React Router v7 for client-side routing, Zustand for WebSocket progress state, Zod schemas as the API contract, and Tailwind CSS v4 for styling. Each domain follows the pattern: `lib/schemas/{domain}.ts` → `api/{domain}.ts` → `components/domain/` → `pages/`. Zod schemas are the single source of truth for TypeScript types (via `z.infer`).

**Tech Stack:** React 19, TypeScript 5.6, Vite 6, TanStack Query v5, React Router v7, Tailwind CSS v4, Zod v3, Lucide React, TanStack Virtual, Zustand

---

## Conventions

### API base URL

Backend runs at `http://127.0.0.1:8000`. All API calls go through `api/client.ts` which prepends this. The Vite dev server runs at port 5173 — no proxy needed since we add CORS or use direct fetch.

### Auth header

Every API call includes `X-Airwave-Token: dev-token` (hardcoded for this single-user tool, read from `client.ts`).

### Path alias

`@/` maps to `frontend/src/` — use `@/api/client` not `../../api/client`.

### Verification per task

```bash
cd frontend && npx tsc --noEmit     # TypeScript compiles
cd frontend && npm run dev           # Start dev server, verify in browser
```

### Commit convention

```bash
git add frontend/src/...
git commit -m "feat(frontend): <description>"
```

---

## File Map

### Infrastructure (Task 1)
| File | Responsibility |
|------|---------------|
| `frontend/src/index.css` | Tailwind v4 import + base styles |
| `frontend/src/main.tsx` | Router + QueryClient + App mount |
| `frontend/src/App.tsx` | Shell: Sidebar + Outlet + ProgressBar |
| `frontend/src/lib/utils.ts` | formatDuration, formatDate, formatConfidence |

### Layout Components (Task 1)
| File | Responsibility |
|------|---------------|
| `frontend/src/components/layout/Sidebar.tsx` | Navigation sidebar |
| `frontend/src/components/layout/ProgressBar.tsx` | WebSocket-driven task progress |

### WebSocket + State (Task 1)
| File | Responsibility |
|------|---------------|
| `frontend/src/hooks/useWebSocket.ts` | WebSocket with exponential backoff |
| `frontend/src/store/progressStore.ts` | Zustand store for task progress |
| `frontend/src/lib/schemas/tasks.ts` | Task Zod schemas (populate stub) |
| `frontend/src/api/tasks.ts` | useActiveTasks() hook |

### Shared UI (created as needed)
| File | Responsibility |
|------|---------------|
| `frontend/src/components/ui/Badge.tsx` | Status badge with semantic colors |
| `frontend/src/components/ui/Spinner.tsx` | Loading spinner |
| `frontend/src/components/ui/EmptyState.tsx` | Empty list placeholder |
| `frontend/src/components/ui/PageHeader.tsx` | Page title + actions slot |
| `frontend/src/components/ui/Modal.tsx` | Dialog overlay |
| `frontend/src/components/ui/DataTable.tsx` | Reusable sortable table |

### Stations Domain (Tasks 2–4)
| File | Responsibility |
|------|---------------|
| `frontend/src/lib/schemas/stations.ts` | Station Zod schemas (populate stub) |
| `frontend/src/api/client.ts` | Base fetcher + error hierarchy |
| `frontend/src/api/stations.ts` | Station CRUD hooks |
| `frontend/src/api/ingestion.ts` | CSV upload hook |
| `frontend/src/components/domain/stations/StationForm.tsx` | Add/Edit form |
| `frontend/src/pages/stations/StationList.tsx` | Station list page |
| `frontend/src/pages/stations/StationDashboard.tsx` | Station detail + playlists |

### Playlists Domain (Tasks 5–6)
| File | Responsibility |
|------|---------------|
| `frontend/src/lib/schemas/playlists.ts` | Playlist Zod schemas |
| `frontend/src/api/playlists.ts` | Playlist hooks |
| `frontend/src/components/domain/playlists/PlaylistEventTable.tsx` | Paginated events |
| `frontend/src/components/domain/playlists/DatePicker.tsx` | Calendar with broadcast days |
| `frontend/src/pages/stations/PlaylistViewer.tsx` | Playlist detail page |

### Matcher Domain (Tasks 7–10)
| File | Responsibility |
|------|---------------|
| `frontend/src/lib/schemas/matcher.ts` | Matcher Zod schemas |
| `frontend/src/lib/schemas/matches.ts` | Match candidate schemas |
| `frontend/src/api/matcher.ts` | Matching queue + resolution hooks |
| `frontend/src/components/domain/matcher/ArtistPanel.tsx` | Artist resolution step |
| `frontend/src/components/domain/matcher/TitlePanel.tsx` | Title/file resolution step |
| `frontend/src/components/domain/matcher/SearchSlideOver.tsx` | Artist + file search |
| `frontend/src/pages/matcher/MatcherBrowser.tsx` | Main matcher page |
| `frontend/src/pages/matcher/ScannerActions.tsx` | Scan + import actions |

### Library Domain (Tasks 11–16)
| File | Responsibility |
|------|---------------|
| `frontend/src/lib/schemas/library.ts` | Library Zod schemas |
| `frontend/src/lib/schemas/artists.ts` | Artist detail schemas |
| `frontend/src/lib/schemas/works.ts` | Work detail schemas |
| `frontend/src/api/library.ts` | Library status + scan hooks |
| `frontend/src/api/artists.ts` | Artist list + detail hooks |
| `frontend/src/api/works.ts` | Work detail + master + override hooks |
| `frontend/src/pages/library/LibraryStatus.tsx` | Library overview |
| `frontend/src/pages/library/ArtistBrowser.tsx` | Virtual scrolling artist list |
| `frontend/src/pages/library/ArtistDetail.tsx` | Artist with works |
| `frontend/src/components/domain/library/FeaturedReleasesSection.tsx` | Featured releases |
| `frontend/src/components/domain/works/WorkFilesTable.tsx` | Files per recording |
| `frontend/src/components/domain/works/FormatOverridePanel.tsx` | Format override CRUD |
| `frontend/src/pages/library/AssociatedWorks.tsx` | Work detail with master toggle |

### Settings Domain (Task 17)
| File | Responsibility |
|------|---------------|
| `frontend/src/lib/schemas/settings.ts` | Settings Zod schemas |
| `frontend/src/api/settings.ts` | Settings hooks |
| `frontend/src/pages/settings/Settings.tsx` | Settings page |
| `frontend/src/pages/settings/PathConfiguration.tsx` | Path config page |

---

## Task 1: App Shell + WebSocket + Progress Bar

**Files:**
- Create: `src/index.css`, `src/lib/utils.ts`, `src/hooks/useWebSocket.ts`, `src/store/progressStore.ts`, `src/components/layout/Sidebar.tsx`, `src/components/layout/ProgressBar.tsx`, `src/components/ui/Spinner.tsx`, `src/App.tsx`
- Modify: `src/main.tsx`, `src/lib/schemas/tasks.ts`, `src/lib/schemas/index.ts`
- Create: `src/api/tasks.ts`

### Step 1 — Global CSS

- [ ] **Step 1a: Create `src/index.css`**

```css
@import "tailwindcss";

@layer base {
  body {
    @apply bg-gray-50 text-gray-900 antialiased;
  }
}
```

### Step 2 — Utility functions

- [ ] **Step 2a: Create `src/lib/utils.ts`**

```typescript
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || ms <= 0) return "—";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatConfidence(score: number | null | undefined): string {
  if (score == null) return "—";
  return `${Math.round(score * 100)}%`;
}

export function cn(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}
```

### Step 3 — Populate tasks schema

- [ ] **Step 3a: Replace `src/lib/schemas/tasks.ts`**

```typescript
import { z } from "zod";

export const TaskInfoSchema = z.object({
  task_id: z.string(),
  task_type: z.string(),
  status: z.string(),
  progress_data: z.record(z.string(), z.unknown()),
  started_at: z.string(),
  updated_at: z.string(),
  completed_at: z.string().nullable(),
});

export type TaskInfo = z.infer<typeof TaskInfoSchema>;

export const TaskListSchema = z.array(TaskInfoSchema);
export type TaskList = z.infer<typeof TaskListSchema>;
```

- [ ] **Step 3b: Update `src/lib/schemas/index.ts`**

Replace with proper re-exports (add as schemas get populated):

```typescript
export * from "./tasks";
```

### Step 4 — Tasks API hook

- [ ] **Step 4a: Create `src/api/tasks.ts`**

```typescript
import { useQuery } from "@tanstack/react-query";

import type { TaskList } from "@/lib/schemas/tasks";

const API_BASE = "http://127.0.0.1:8000";
const TOKEN = "dev-token";

export function useActiveTasks() {
  return useQuery<TaskList>({
    queryKey: ["tasks", "active"],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/api/v1/tasks/active`, {
        headers: { "X-Airwave-Token": TOKEN },
      });
      if (!res.ok) throw new Error(`Tasks fetch failed: ${res.status}`);
      return res.json() as Promise<TaskList>;
    },
    refetchInterval: 5000,
  });
}
```

### Step 5 — WebSocket hook

- [ ] **Step 5a: Create `src/hooks/useWebSocket.ts`**

```typescript
import { useEffect, useRef, useCallback } from "react";

import { useQueryClient } from "@tanstack/react-query";

import { useProgressStore } from "@/store/progressStore";
import type { TaskInfo } from "@/lib/schemas/tasks";

const WS_URL = "ws://127.0.0.1:8000/ws?token=dev-token";
const MIN_BACKOFF = 1000;
const MAX_BACKOFF = 30000;
const JITTER = 0.2;

interface WsMessage {
  tasks: TaskInfo[];
}

export function useWebSocket(): void {
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(MIN_BACKOFF);
  const setTasks = useProgressStore((s) => s.setTasks);
  const queryClient = useQueryClient();

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      backoffRef.current = MIN_BACKOFF;
      // On reconnect, immediately refresh active tasks
      queryClient.invalidateQueries({ queryKey: ["tasks", "active"] });
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WsMessage;
        setTasks(data.tasks);
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      const delay = backoffRef.current * (1 + (Math.random() * 2 - 1) * JITTER);
      backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF);
      setTimeout(connect, delay);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [setTasks, queryClient]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);
}
```

### Step 6 — Progress store

- [ ] **Step 6a: Create `src/store/progressStore.ts`**

```typescript
import { create } from "zustand";

import type { TaskInfo } from "@/lib/schemas/tasks";

type ProgressState = "IDLE" | "RUNNING" | "COMPLETED" | "FAILED";

interface ProgressStore {
  tasks: TaskInfo[];
  state: ProgressState;
  activeTask: TaskInfo | null;
  extraCount: number;
  setTasks: (tasks: TaskInfo[]) => void;
  dismiss: () => void;
}

export const useProgressStore = create<ProgressStore>((set, get) => ({
  tasks: [],
  state: "IDLE",
  activeTask: null,
  extraCount: 0,

  setTasks: (tasks: TaskInfo[]) => {
    const prev = get();
    const running = tasks.filter((t) => t.status === "running");
    const failed = tasks.filter((t) => t.status === "failed");

    if (running.length > 0) {
      // Show most recently started task
      const sorted = [...running].sort(
        (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
      );
      set({
        tasks,
        state: "RUNNING",
        activeTask: sorted[0],
        extraCount: sorted.length - 1,
      });
    } else if (prev.state === "RUNNING") {
      // Was running, now done — check for failures
      if (failed.length > 0) {
        set({ tasks, state: "FAILED", activeTask: failed[0], extraCount: 0 });
      } else {
        set({ tasks, state: "COMPLETED", activeTask: prev.activeTask, extraCount: 0 });
        // Auto-dismiss after 2s
        setTimeout(() => {
          if (get().state === "COMPLETED") {
            set({ state: "IDLE", activeTask: null, extraCount: 0 });
          }
        }, 2000);
      }
    } else {
      set({ tasks });
    }
  },

  dismiss: () => {
    set({ state: "IDLE", activeTask: null, extraCount: 0 });
  },
}));
```

### Step 7 — Spinner component

- [ ] **Step 7a: Create `src/components/ui/Spinner.tsx`**

```tsx
export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      className={`animate-spin text-current ${className}`}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}
```

### Step 8 — ProgressBar component

- [ ] **Step 8a: Create `src/components/layout/ProgressBar.tsx`**

```tsx
import { CheckCircle, XCircle, Loader2, X } from "lucide-react";

import { useProgressStore } from "@/store/progressStore";
import { cn } from "@/lib/utils";

const TASK_TYPE_LABELS: Record<string, string> = {
  scan: "Scanning library",
  enrichment: "Enriching metadata",
  ingestion: "Importing playlist",
  matching: "Running matcher",
  m3u_export: "Exporting M3U",
  rules_apply: "Applying rules",
};

export function ProgressBar() {
  const { state, activeTask, extraCount, dismiss } = useProgressStore();

  if (state === "IDLE" || !activeTask) return null;

  const label = TASK_TYPE_LABELS[activeTask.task_type] ?? activeTask.task_type;
  const progress = activeTask.progress_data;
  const pct = typeof progress.percent === "number" ? progress.percent : null;

  return (
    <div
      className={cn(
        "fixed bottom-0 left-64 right-0 z-50 border-t px-4 py-2 flex items-center gap-3 text-sm",
        state === "RUNNING" && "bg-blue-50 border-blue-200 text-blue-800",
        state === "COMPLETED" && "bg-green-50 border-green-200 text-green-800",
        state === "FAILED" && "bg-red-50 border-red-200 text-red-800",
      )}
    >
      {state === "RUNNING" && <Loader2 className="h-4 w-4 animate-spin" />}
      {state === "COMPLETED" && <CheckCircle className="h-4 w-4" />}
      {state === "FAILED" && <XCircle className="h-4 w-4" />}

      <span className="font-medium">{label}</span>

      {pct !== null && state === "RUNNING" && (
        <div className="flex-1 max-w-xs">
          <div className="h-1.5 rounded-full bg-blue-200">
            <div
              className="h-1.5 rounded-full bg-blue-600 transition-all"
              style={{ width: `${Math.min(pct, 100)}%` }}
            />
          </div>
        </div>
      )}

      {extraCount > 0 && (
        <span className="text-xs opacity-70">+{extraCount} more</span>
      )}

      {state === "FAILED" && (
        <button
          onClick={dismiss}
          className="ml-auto p-1 hover:bg-red-100 rounded"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  );
}
```

### Step 9 — Sidebar

- [ ] **Step 9a: Create `src/components/layout/Sidebar.tsx`**

```tsx
import { NavLink } from "react-router-dom";
import { Radio, Library, GitCompare, Settings, Music } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/stations", label: "Stations", icon: Radio },
  { to: "/library", label: "Library", icon: Library },
  { to: "/matcher", label: "Matcher", icon: GitCompare },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

export function Sidebar() {
  return (
    <aside className="fixed inset-y-0 left-0 w-64 bg-gray-900 text-gray-100 flex flex-col">
      <div className="h-16 flex items-center gap-3 px-6 border-b border-gray-800">
        <Music className="h-6 w-6 text-purple-400" />
        <span className="text-lg font-bold tracking-tight">RetroStation</span>
      </div>

      <nav className="flex-1 py-4 space-y-1 px-3">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors",
                isActive
                  ? "bg-gray-800 text-white"
                  : "text-gray-400 hover:bg-gray-800/50 hover:text-gray-200",
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
```

### Step 10 — App shell

- [ ] **Step 10a: Create `src/App.tsx`**

```tsx
import { Outlet } from "react-router-dom";

import { Sidebar } from "@/components/layout/Sidebar";
import { ProgressBar } from "@/components/layout/ProgressBar";
import { useWebSocket } from "@/hooks/useWebSocket";

export default function App() {
  useWebSocket();

  return (
    <div className="min-h-screen">
      <Sidebar />
      <main className="ml-64 p-6">
        <Outlet />
      </main>
      <ProgressBar />
    </div>
  );
}
```

### Step 11 — Main entry + router

- [ ] **Step 11a: Replace `src/main.tsx`**

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import App from "@/App";
import "@/index.css";

// Placeholder pages — replaced in subsequent tasks
function Placeholder({ name }: { name: string }) {
  return <div className="text-gray-400 text-sm">{name} — coming soon</div>;
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1 },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/stations" replace /> },
      {
        path: "stations",
        children: [
          { index: true, element: <Placeholder name="StationList" /> },
          { path: ":station_id", element: <Placeholder name="StationDashboard" /> },
          { path: ":station_id/playlists", element: <Placeholder name="PlaylistViewer" /> },
        ],
      },
      {
        path: "library",
        children: [
          { index: true, element: <Placeholder name="LibraryStatus" /> },
          { path: "artists", element: <Placeholder name="ArtistBrowser" /> },
          { path: "artists/:artist_id", element: <Placeholder name="ArtistDetail" /> },
          { path: "artists/:artist_id/works/:work_id", element: <Placeholder name="AssociatedWorks" /> },
        ],
      },
      {
        path: "matcher",
        children: [
          { index: true, element: <Placeholder name="MatcherBrowser" /> },
          { path: "scanner", element: <Placeholder name="ScannerActions" /> },
        ],
      },
      {
        path: "settings",
        children: [
          { index: true, element: <Placeholder name="Settings" /> },
          { path: "paths", element: <Placeholder name="PathConfiguration" /> },
        ],
      },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
```

### Step 12 — Verify + commit

- [ ] **Step 12a: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors.

- [ ] **Step 12b: Visual verification**

Run: `cd frontend && npm run dev`
Open: `http://localhost:5173`
Expected: Dark sidebar with 4 nav links, "StationList — coming soon" in content area. Progress bar hidden (no running tasks).

- [ ] **Step 12c: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): app shell — sidebar, router, WebSocket progress bar, Zustand store"
```

---

## Task 2: API Client + Stations Schemas/Hooks + StationForm

**Files:**
- Create: `src/api/client.ts`, `src/api/stations.ts`, `src/api/ingestion.ts`
- Create: `src/components/domain/stations/StationForm.tsx`
- Create: `src/components/ui/Badge.tsx`, `src/components/ui/PageHeader.tsx`, `src/components/ui/EmptyState.tsx`, `src/components/ui/Modal.tsx`
- Modify: `src/lib/schemas/stations.ts`, `src/lib/schemas/index.ts`

### Step 1 — API client with error hierarchy

- [ ] **Step 1a: Create `src/api/client.ts`**

```typescript
const API_BASE = "http://127.0.0.1:8000";
const TOKEN = "dev-token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
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
  public detail: unknown[];
  constructor(detail: unknown[]) {
    super(422, "Validation error");
    this.name = "ValidationError";
    this.detail = detail;
  }
}

export class ServerError extends ApiError {
  constructor(status: number, message = "Server error") {
    super(status, message);
    this.name = "ServerError";
  }
}

async function handleError(res: Response): Promise<never> {
  let body: Record<string, unknown> = {};
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    // No JSON body
  }
  const msg = typeof body.detail === "string" ? body.detail : res.statusText;

  if (res.status === 401) throw new AuthError(msg);
  if (res.status === 409) throw new ConflictError(msg);
  if (res.status === 422) throw new ValidationError(body.detail as unknown[] ?? []);
  if (res.status >= 500) throw new ServerError(res.status, msg);
  throw new ApiError(res.status, msg);
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "X-Airwave-Token": TOKEN,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!res.ok) return handleError(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function apiUpload<T>(
  path: string,
  formData: FormData,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "X-Airwave-Token": TOKEN },
    body: formData,
  });
  if (!res.ok) return handleError(res);
  return res.json() as Promise<T>;
}

export async function apiDownload(path: string, body?: unknown): Promise<Blob> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      "X-Airwave-Token": TOKEN,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) return handleError(res);
  return res.blob();
}
```

### Step 2 — Stations schemas

- [ ] **Step 2a: Replace `src/lib/schemas/stations.ts`**

```typescript
import { z } from "zod";

export const StationResponseSchema = z.object({
  id: z.string().uuid(),
  call_letters: z.string(),
  name: z.string().nullable(),
  city: z.string().nullable(),
  format_name: z.string().nullable(),
  created_at: z.string(),
});
export type StationResponse = z.infer<typeof StationResponseSchema>;

export const StationSummarySchema = StationResponseSchema.extend({
  playlist_count: z.number(),
});
export type StationSummary = z.infer<typeof StationSummarySchema>;

export const StationListSchema = z.array(StationSummarySchema);
export type StationList = z.infer<typeof StationListSchema>;

export const StationCreateSchema = z.object({
  call_letters: z.string().min(1),
  name: z.string().nullable().optional(),
  city: z.string().nullable().optional(),
  format_name: z.string().nullable().optional(),
});
export type StationCreate = z.infer<typeof StationCreateSchema>;

export const StationUpdateSchema = z.object({
  call_letters: z.string().optional(),
  name: z.string().nullable().optional(),
  city: z.string().nullable().optional(),
  format_name: z.string().nullable().optional(),
});
export type StationUpdate = z.infer<typeof StationUpdateSchema>;

// Re-export for backwards compat with stub names
export const StationSchema = StationResponseSchema;
export const StationDashboardSchema = StationResponseSchema;
```

- [ ] **Step 2b: Update `src/lib/schemas/index.ts`**

```typescript
export * from "./tasks";
export * from "./stations";
```

### Step 3 — Stations API hooks

- [ ] **Step 3a: Create `src/api/stations.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type {
  StationList,
  StationResponse,
  StationCreate,
  StationUpdate,
} from "@/lib/schemas/stations";

export function useStations() {
  return useQuery<StationList>({
    queryKey: ["stations"],
    queryFn: () => apiFetch<StationList>("/api/v1/stations"),
  });
}

export function useStation(id: string | undefined) {
  return useQuery<StationResponse>({
    queryKey: ["stations", id],
    queryFn: () => apiFetch<StationResponse>(`/api/v1/stations/${id}`),
    enabled: !!id,
  });
}

export function useCreateStation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: StationCreate) =>
      apiFetch<StationResponse>("/api/v1/stations", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["stations"] }),
  });
}

export function useUpdateStation(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: StationUpdate) =>
      apiFetch<StationResponse>(`/api/v1/stations/${id}`, {
        method: "PUT",
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stations"] });
      qc.invalidateQueries({ queryKey: ["stations", id] });
    },
  });
}

export function useDeleteStation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<void>(`/api/v1/stations/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["stations"] }),
  });
}
```

### Step 4 — Shared UI components

- [ ] **Step 4a: Create `src/components/ui/Badge.tsx`**

```tsx
import { cn } from "@/lib/utils";

type Variant = "default" | "success" | "warning" | "danger" | "info";

const VARIANTS: Record<Variant, string> = {
  default: "bg-gray-100 text-gray-700",
  success: "bg-green-100 text-green-700",
  warning: "bg-yellow-100 text-yellow-700",
  danger: "bg-red-100 text-red-700",
  info: "bg-blue-100 text-blue-700",
};

export function Badge({
  children,
  variant = "default",
  className,
}: {
  children: React.ReactNode;
  variant?: Variant;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium",
        VARIANTS[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}

const STATUS_VARIANTS: Record<string, Variant> = {
  AUTO_MATCHED: "success",
  MAN_MATCHED: "success",
  NEEDS_REVIEW: "warning",
  PENDING: "info",
  AUTO_REJECTED: "danger",
  MAN_REJECTED: "danger",
};

export function MatchStatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={STATUS_VARIANTS[status] ?? "default"}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}
```

- [ ] **Step 4b: Create `src/components/ui/PageHeader.tsx`**

```tsx
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">{title}</h1>
        {description && (
          <p className="mt-1 text-sm text-gray-500">{description}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
```

- [ ] **Step 4c: Create `src/components/ui/EmptyState.tsx`**

```tsx
import { Inbox } from "lucide-react";

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="text-center py-12">
      <Inbox className="mx-auto h-12 w-12 text-gray-400" />
      <h3 className="mt-2 text-sm font-medium text-gray-900">{title}</h3>
      {description && (
        <p className="mt-1 text-sm text-gray-500">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
```

- [ ] **Step 4d: Create `src/components/ui/Modal.tsx`**

```tsx
import { useEffect, useRef } from "react";
import { X } from "lucide-react";

export function Modal({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      className="backdrop:bg-black/50 rounded-lg shadow-xl p-0 max-w-lg w-full"
    >
      <div className="flex items-center justify-between px-6 py-4 border-b">
        <h2 className="text-lg font-semibold">{title}</h2>
        <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="px-6 py-4">{children}</div>
    </dialog>
  );
}
```

### Step 5 — Ingestion API

- [ ] **Step 5a: Create `src/api/ingestion.ts`**

```typescript
import { useMutation } from "@tanstack/react-query";

import { apiUpload } from "@/api/client";

export function useUploadPlaylist() {
  return useMutation({
    mutationFn: async ({
      file,
      stationId,
    }: {
      file: File;
      stationId: string;
    }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("station_id", stationId);
      return apiUpload<{ status: string; message: string }>(
        "/api/v1/ingestion/playlists",
        form,
      );
    },
  });
}
```

### Step 6 — StationForm component

- [ ] **Step 6a: Create `src/components/domain/stations/StationForm.tsx`**

```tsx
import { useState } from "react";
import type { StationCreate, StationResponse } from "@/lib/schemas/stations";

interface StationFormProps {
  initial?: StationResponse;
  onSubmit: (data: StationCreate) => void;
  onCancel: () => void;
  isPending?: boolean;
}

export function StationForm({ initial, onSubmit, onCancel, isPending }: StationFormProps) {
  const [callLetters, setCallLetters] = useState(initial?.call_letters ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [city, setCity] = useState(initial?.city ?? "");
  const [formatName, setFormatName] = useState(initial?.format_name ?? "");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSubmit({
      call_letters: callLetters.trim(),
      name: name.trim() || null,
      city: city.trim() || null,
      format_name: formatName.trim() || null,
    });
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Call Letters <span className="text-red-500">*</span>
        </label>
        <input
          type="text"
          value={callLetters}
          onChange={(e) => setCallLetters(e.target.value)}
          placeholder="e.g. KAZR-FM"
          required
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Station Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Laser 103.3"
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">City</label>
        <input
          type="text"
          value={city}
          onChange={(e) => setCity(e.target.value)}
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Format</label>
        <input
          type="text"
          value={formatName}
          onChange={(e) => setFormatName(e.target.value)}
          placeholder="e.g. CHR, AC, Rock"
          className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        {initial?.format_name && formatName !== initial.format_name && (
          <p className="mt-1 text-xs text-amber-600">
            Changing the format name may affect existing format overrides.
          </p>
        )}
      </div>

      <div className="flex justify-end gap-3 pt-2">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 rounded-md"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={isPending || !callLetters.trim()}
          className="px-4 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-md disabled:opacity-50"
        >
          {isPending ? "Saving..." : initial ? "Update" : "Create"}
        </button>
      </div>
    </form>
  );
}
```

### Step 7 — Verify + commit

- [ ] **Step 7a: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors.

- [ ] **Step 7b: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): API client, stations schemas/hooks, StationForm, shared UI components"
```

---

## Task 3: StationList Page

**Files:**
- Create: `src/pages/stations/StationList.tsx`
- Modify: `src/main.tsx` (replace Placeholder with real page)

### Step 1 — StationList page

- [ ] **Step 1a: Create `src/pages/stations/StationList.tsx`**

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import { Plus, Radio, Trash2 } from "lucide-react";

import { useStations, useCreateStation, useDeleteStation } from "@/api/stations";
import { PageHeader } from "@/components/ui/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { Spinner } from "@/components/ui/Spinner";
import { Modal } from "@/components/ui/Modal";
import { StationForm } from "@/components/domain/stations/StationForm";
import type { StationCreate } from "@/lib/schemas/stations";

export default function StationList() {
  const { data: stations, isLoading } = useStations();
  const createStation = useCreateStation();
  const deleteStation = useDeleteStation();
  const [showCreate, setShowCreate] = useState(false);

  function handleCreate(data: StationCreate) {
    createStation.mutate(data, { onSuccess: () => setShowCreate(false) });
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  return (
    <>
      <PageHeader
        title="Stations"
        description="Manage radio stations and their playlists"
        actions={
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-md"
          >
            <Plus className="h-4 w-4" /> Add Station
          </button>
        }
      />

      {!stations?.length ? (
        <EmptyState
          title="No stations yet"
          description="Add a radio station to get started."
          action={
            <button
              onClick={() => setShowCreate(true)}
              className="text-sm text-blue-600 hover:text-blue-700 font-medium"
            >
              Add your first station
            </button>
          }
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {stations.map((station) => (
            <Link
              key={station.id}
              to={`/stations/${station.id}`}
              className="block rounded-lg border border-gray-200 bg-white p-5 hover:shadow-md transition-shadow"
            >
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="p-2 bg-purple-50 rounded-lg">
                    <Radio className="h-5 w-5 text-purple-600" />
                  </div>
                  <div>
                    <h3 className="font-semibold text-gray-900">
                      {station.call_letters}
                    </h3>
                    {station.name && (
                      <p className="text-sm text-gray-500">{station.name}</p>
                    )}
                  </div>
                </div>
                <button
                  onClick={(e) => {
                    e.preventDefault();
                    if (confirm(`Delete ${station.call_letters}?`)) {
                      deleteStation.mutate(station.id);
                    }
                  }}
                  className="p-1 text-gray-400 hover:text-red-500 rounded"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
              <div className="mt-3 flex gap-4 text-xs text-gray-500">
                {station.city && <span>{station.city}</span>}
                {station.format_name && (
                  <span className="px-1.5 py-0.5 bg-gray-100 rounded">
                    {station.format_name}
                  </span>
                )}
                <span>{station.playlist_count} playlist(s)</span>
              </div>
            </Link>
          ))}
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="Add Station">
        <StationForm
          onSubmit={handleCreate}
          onCancel={() => setShowCreate(false)}
          isPending={createStation.isPending}
        />
      </Modal>
    </>
  );
}
```

### Step 2 — Wire into router

- [ ] **Step 2a: Update `src/main.tsx`**

Replace the stations index route placeholder:

```typescript
// At top, add import:
import StationList from "@/pages/stations/StationList";

// In router, change:
// { index: true, element: <Placeholder name="StationList" /> },
// to:
{ index: true, element: <StationList /> },
```

### Step 3 — Verify + commit

- [ ] **Step 3a: Typecheck**

Run: `cd frontend && npx tsc --noEmit`

- [ ] **Step 3b: Visual verification**

Run dev server. Navigate to `/stations`. Verify:
- Empty state shows when no stations exist
- "Add Station" button opens modal with form
- Creating a station shows it in the grid
- Station cards show call letters, name, city, format, playlist count

- [ ] **Step 3c: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): StationList page — station cards with create/delete"
```

---

## Task 4: StationDashboard Page

**Files:**
- Create: `src/pages/stations/StationDashboard.tsx`
- Modify: `src/main.tsx` (replace placeholder)

### Step 1 — StationDashboard

- [ ] **Step 1a: Create `src/pages/stations/StationDashboard.tsx`**

```tsx
import { useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { ArrowLeft, Upload, Edit2, ListMusic } from "lucide-react";

import { useStation, useUpdateStation } from "@/api/stations";
import { useUploadPlaylist } from "@/api/ingestion";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { Modal } from "@/components/ui/Modal";
import { StationForm } from "@/components/domain/stations/StationForm";
import { formatDateTime } from "@/lib/utils";
import type { StationUpdate } from "@/lib/schemas/stations";

export default function StationDashboard() {
  const { station_id } = useParams<{ station_id: string }>();
  const navigate = useNavigate();
  const { data: station, isLoading } = useStation(station_id);
  const updateStation = useUpdateStation(station_id!);
  const uploadPlaylist = useUploadPlaylist();
  const [showEdit, setShowEdit] = useState(false);

  function handleUpdate(data: StationUpdate) {
    updateStation.mutate(data, { onSuccess: () => setShowEdit(false) });
  }

  function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || !station_id) return;
    uploadPlaylist.mutate(
      { file, stationId: station_id },
      {
        onSuccess: () => {
          e.target.value = "";
        },
      },
    );
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  if (!station) {
    return <div className="text-gray-500">Station not found</div>;
  }

  return (
    <>
      <div className="mb-4">
        <button
          onClick={() => navigate("/stations")}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" /> Back to stations
        </button>
      </div>

      <PageHeader
        title={station.call_letters}
        description={[station.name, station.city].filter(Boolean).join(" — ")}
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowEdit(true)}
              className="flex items-center gap-2 px-3 py-2 text-sm border rounded-md hover:bg-gray-50"
            >
              <Edit2 className="h-4 w-4" /> Edit
            </button>
            <label className="flex items-center gap-2 px-3 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-md cursor-pointer">
              <Upload className="h-4 w-4" /> Import CSV
              <input
                type="file"
                accept=".csv"
                onChange={handleUpload}
                className="hidden"
              />
            </label>
          </div>
        }
      />

      {/* Info cards */}
      <div className="grid gap-4 sm:grid-cols-3 mb-8">
        <div className="bg-white rounded-lg border p-4">
          <div className="text-sm text-gray-500">Format</div>
          <div className="text-lg font-semibold">{station.format_name ?? "—"}</div>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <div className="text-sm text-gray-500">Created</div>
          <div className="text-lg font-semibold">{formatDateTime(station.created_at)}</div>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <Link
            to={`/stations/${station.id}/playlists`}
            className="flex items-center gap-2 text-blue-600 hover:text-blue-700"
          >
            <ListMusic className="h-5 w-5" />
            <span className="text-lg font-semibold">View Playlists</span>
          </Link>
        </div>
      </div>

      {uploadPlaylist.isPending && (
        <div className="flex items-center gap-2 text-sm text-blue-600 mb-4">
          <Spinner /> Uploading playlist...
        </div>
      )}
      {uploadPlaylist.isSuccess && (
        <div className="text-sm text-green-600 mb-4">
          Playlist uploaded! Check progress bar for ingestion status.
        </div>
      )}

      <Modal open={showEdit} onClose={() => setShowEdit(false)} title="Edit Station">
        <StationForm
          initial={station}
          onSubmit={handleUpdate}
          onCancel={() => setShowEdit(false)}
          isPending={updateStation.isPending}
        />
      </Modal>
    </>
  );
}
```

### Step 2 — Wire into router

- [ ] **Step 2a: Update `src/main.tsx`**

```typescript
import StationDashboard from "@/pages/stations/StationDashboard";

// Replace: { path: ":station_id", element: <Placeholder name="StationDashboard" /> },
// With:    { path: ":station_id", element: <StationDashboard /> },
```

### Step 3 — Verify + commit

- [ ] **Step 3a: Typecheck + visual verification**

Navigate to a station. Verify: back link, edit button, CSV upload, info cards, playlists link.

- [ ] **Step 3b: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): StationDashboard — station detail with edit and CSV upload"
```

---

## Task 5: Playlists Schemas/Hooks + PlaylistEventTable + DatePicker

**Files:**
- Modify: `src/lib/schemas/playlists.ts`
- Create: `src/api/playlists.ts`
- Create: `src/components/domain/playlists/PlaylistEventTable.tsx`
- Create: `src/components/domain/playlists/DatePicker.tsx`
- Modify: `src/lib/schemas/index.ts`

### Step 1 — Playlists schemas

- [ ] **Step 1a: Replace `src/lib/schemas/playlists.ts`**

```typescript
import { z } from "zod";

export const PlaylistSummarySchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  station_id: z.string().uuid().nullable(),
  content_hash: z.string(),
  ingested_at: z.string(),
  event_count: z.number(),
});
export type PlaylistSummary = z.infer<typeof PlaylistSummarySchema>;

export const PlaylistDetailSchema = z.object({
  id: z.string().uuid(),
  name: z.string(),
  station_id: z.string().uuid().nullable(),
  content_hash: z.string(),
  ingested_at: z.string(),
});
export type PlaylistDetail = z.infer<typeof PlaylistDetailSchema>;

export const EventItemSchema = z.object({
  id: z.string().uuid(),
  played_at: z.string(),
  artist_name: z.string(),
  title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
});
export type EventItem = z.infer<typeof EventItemSchema>;

export const PaginatedEventsSchema = z.object({
  items: z.array(EventItemSchema),
  total: z.number(),
});
export type PaginatedEvents = z.infer<typeof PaginatedEventsSchema>;

// Stub compat
export const PlaylistSchema = PlaylistSummarySchema;
export const PlaylistEventSchema = EventItemSchema;
export const ExportResultSchema = z.object({ status: z.string() });
```

- [ ] **Step 1b: Update `src/lib/schemas/index.ts`**

Add: `export * from "./playlists";`

### Step 2 — Playlists API hooks

- [ ] **Step 2a: Create `src/api/playlists.ts`**

```typescript
import { useQuery, useMutation } from "@tanstack/react-query";

import { apiFetch, apiDownload } from "@/api/client";
import type { PlaylistSummary, PaginatedEvents } from "@/lib/schemas/playlists";

export function usePlaylists(stationId: string | undefined) {
  return useQuery<PlaylistSummary[]>({
    queryKey: ["playlists", stationId],
    queryFn: () =>
      apiFetch<PlaylistSummary[]>(`/api/v1/playlists?station_id=${stationId}`),
    enabled: !!stationId,
  });
}

export function usePlaylistEvents(
  playlistId: string | undefined,
  limit: number,
  offset: number,
) {
  return useQuery<PaginatedEvents>({
    queryKey: ["playlists", playlistId, "events", limit, offset],
    queryFn: () =>
      apiFetch<PaginatedEvents>(
        `/api/v1/playlists/${playlistId}/events?limit=${limit}&offset=${offset}`,
      ),
    enabled: !!playlistId,
  });
}

export function useBroadcastDays(playlistId: string | undefined) {
  return useQuery<string[]>({
    queryKey: ["playlists", playlistId, "broadcast-days"],
    queryFn: () =>
      apiFetch<string[]>(`/api/v1/playlists/${playlistId}/broadcast-days`),
    enabled: !!playlistId,
  });
}

export function useExportM3u() {
  return useMutation({
    mutationFn: async ({
      playlistId,
      stationFormat,
    }: {
      playlistId: string;
      stationFormat?: string;
    }) => {
      const blob = await apiDownload(
        `/api/v1/playlists/${playlistId}/export-m3u`,
        stationFormat ? { station_format: stationFormat } : undefined,
      );
      // Trigger download
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `playlist-${playlistId}.m3u`;
      a.click();
      URL.revokeObjectURL(url);
    },
  });
}
```

### Step 3 — PlaylistEventTable

- [ ] **Step 3a: Create `src/components/domain/playlists/PlaylistEventTable.tsx`**

```tsx
import { useState } from "react";

import { usePlaylistEvents } from "@/api/playlists";
import { MatchStatusBadge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { formatDateTime } from "@/lib/utils";

const PAGE_SIZE = 50;

export function PlaylistEventTable({ playlistId }: { playlistId: string }) {
  const [page, setPage] = useState(0);
  const offset = page * PAGE_SIZE;
  const { data, isLoading } = usePlaylistEvents(playlistId, PAGE_SIZE, offset);

  if (isLoading) {
    return (
      <div className="flex justify-center py-8">
        <Spinner className="h-6 w-6 text-gray-400" />
      </div>
    );
  }

  if (!data?.items.length) {
    return <div className="text-sm text-gray-400 py-4">No events found.</div>;
  }

  const totalPages = Math.ceil(data.total / PAGE_SIZE);

  return (
    <div>
      <div className="text-xs text-gray-500 mb-2">
        {data.total.toLocaleString()} total events
      </div>

      <div className="overflow-x-auto border rounded-lg">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Time</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Artist</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Title</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Status</th>
              <th className="px-4 py-2 text-left font-medium text-gray-500">Tier</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.items.map((event) => (
              <tr key={event.id} className="hover:bg-gray-50">
                <td className="px-4 py-2 whitespace-nowrap text-gray-500">
                  {formatDateTime(event.played_at)}
                </td>
                <td className="px-4 py-2 font-medium">{event.artist_name}</td>
                <td className="px-4 py-2">{event.title}</td>
                <td className="px-4 py-2">
                  <MatchStatusBadge status={event.match_status} />
                </td>
                <td className="px-4 py-2 text-gray-500 text-xs">
                  {event.match_tier ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between mt-3">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 text-sm border rounded disabled:opacity-50"
          >
            Previous
          </button>
          <span className="text-sm text-gray-500">
            Page {page + 1} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
            className="px-3 py-1 text-sm border rounded disabled:opacity-50"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
```

### Step 4 — DatePicker (broadcast day calendar)

- [ ] **Step 4a: Create `src/components/domain/playlists/DatePicker.tsx`**

```tsx
import { useMemo } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

interface DatePickerProps {
  broadcastDays: string[];
  selectedDate: string | null;
  onSelect: (date: string) => void;
  month: Date;
  onMonthChange: (month: Date) => void;
}

export function DatePicker({
  broadcastDays,
  selectedDate,
  onSelect,
  month,
  onMonthChange,
}: DatePickerProps) {
  const broadcastSet = useMemo(() => new Set(broadcastDays), [broadcastDays]);

  const year = month.getFullYear();
  const mon = month.getMonth();
  const firstDay = new Date(year, mon, 1).getDay();
  const daysInMonth = new Date(year, mon + 1, 0).getDate();

  const cells: (number | null)[] = [];
  for (let i = 0; i < firstDay; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  function toISO(day: number) {
    return `${year}-${String(mon + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }

  return (
    <div className="bg-white border rounded-lg p-4 w-72">
      <div className="flex items-center justify-between mb-3">
        <button
          onClick={() => onMonthChange(new Date(year, mon - 1, 1))}
          className="p-1 hover:bg-gray-100 rounded"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <span className="text-sm font-medium">
          {month.toLocaleDateString("en-US", { month: "long", year: "numeric" })}
        </span>
        <button
          onClick={() => onMonthChange(new Date(year, mon + 1, 1))}
          className="p-1 hover:bg-gray-100 rounded"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      <div className="grid grid-cols-7 gap-1 text-center text-xs">
        {["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"].map((d) => (
          <div key={d} className="py-1 text-gray-400 font-medium">{d}</div>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <div key={`empty-${i}`} />;
          const iso = toISO(day);
          const hasBroadcast = broadcastSet.has(iso);
          const isSelected = iso === selectedDate;

          return (
            <button
              key={iso}
              onClick={() => hasBroadcast && onSelect(iso)}
              disabled={!hasBroadcast}
              className={cn(
                "py-1 rounded text-sm",
                hasBroadcast && "font-medium cursor-pointer",
                hasBroadcast && !isSelected && "text-blue-600 hover:bg-blue-50",
                isSelected && "bg-blue-600 text-white",
                !hasBroadcast && "text-gray-300 cursor-default",
              )}
            >
              {day}
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

### Step 5 — Verify + commit

- [ ] **Step 5a: Typecheck**

Run: `cd frontend && npx tsc --noEmit`

- [ ] **Step 5b: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): playlists schemas/hooks, PlaylistEventTable, DatePicker"
```

---

## Task 6: PlaylistViewer Page

**Files:**
- Create: `src/pages/stations/PlaylistViewer.tsx`
- Modify: `src/main.tsx` (replace placeholder)

### Step 1 — PlaylistViewer

- [ ] **Step 1a: Create `src/pages/stations/PlaylistViewer.tsx`**

```tsx
import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, Download } from "lucide-react";

import { usePlaylists, useBroadcastDays, useExportM3u } from "@/api/playlists";
import { useStation } from "@/api/stations";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { PlaylistEventTable } from "@/components/domain/playlists/PlaylistEventTable";
import { DatePicker } from "@/components/domain/playlists/DatePicker";
import { formatDateTime } from "@/lib/utils";

export default function PlaylistViewer() {
  const { station_id } = useParams<{ station_id: string }>();
  const navigate = useNavigate();
  const { data: station } = useStation(station_id);
  const { data: playlists, isLoading } = usePlaylists(station_id);
  const [selectedPlaylistId, setSelectedPlaylistId] = useState<string | null>(null);
  const { data: broadcastDays } = useBroadcastDays(selectedPlaylistId ?? undefined);
  const exportM3u = useExportM3u();

  const [calMonth, setCalMonth] = useState(() => new Date());
  const [selectedDate, setSelectedDate] = useState<string | null>(null);

  const selected = playlists?.find((p) => p.id === selectedPlaylistId) ?? playlists?.[0];

  // Auto-select first playlist
  if (playlists?.length && !selectedPlaylistId) {
    setSelectedPlaylistId(playlists[0].id);
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  return (
    <>
      <div className="mb-4">
        <button
          onClick={() => navigate(`/stations/${station_id}`)}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" /> Back to {station?.call_letters ?? "station"}
        </button>
      </div>

      <PageHeader
        title="Playlists"
        description={station?.call_letters}
        actions={
          selected && (
            <button
              onClick={() =>
                exportM3u.mutate({
                  playlistId: selected.id,
                  stationFormat: station?.format_name ?? undefined,
                })
              }
              disabled={exportM3u.isPending}
              className="flex items-center gap-2 px-3 py-2 text-sm border rounded-md hover:bg-gray-50"
            >
              <Download className="h-4 w-4" />
              {exportM3u.isPending ? "Exporting..." : "Export M3U"}
            </button>
          )
        }
      />

      {!playlists?.length ? (
        <EmptyState
          title="No playlists"
          description="Import a CSV from the station dashboard."
        />
      ) : (
        <div className="flex gap-6">
          {/* Sidebar: playlist list + calendar */}
          <div className="w-80 shrink-0 space-y-4">
            <div className="border rounded-lg overflow-hidden">
              {playlists.map((pl) => (
                <button
                  key={pl.id}
                  onClick={() => setSelectedPlaylistId(pl.id)}
                  className={`w-full text-left px-4 py-3 text-sm border-b last:border-b-0 ${
                    pl.id === selected?.id ? "bg-blue-50" : "hover:bg-gray-50"
                  }`}
                >
                  <div className="font-medium truncate">{pl.name}</div>
                  <div className="text-xs text-gray-500">
                    {formatDateTime(pl.ingested_at)} — {pl.event_count} events
                  </div>
                </button>
              ))}
            </div>

            {broadcastDays && (
              <DatePicker
                broadcastDays={broadcastDays}
                selectedDate={selectedDate}
                onSelect={setSelectedDate}
                month={calMonth}
                onMonthChange={setCalMonth}
              />
            )}
          </div>

          {/* Main: event table */}
          <div className="flex-1 min-w-0">
            {selected && <PlaylistEventTable playlistId={selected.id} />}
          </div>
        </div>
      )}
    </>
  );
}
```

### Step 2 — Wire into router

- [ ] **Step 2a: Update `src/main.tsx`**

```typescript
import PlaylistViewer from "@/pages/stations/PlaylistViewer";

// Replace: { path: ":station_id/playlists", element: <Placeholder name="PlaylistViewer" /> },
// With:    { path: ":station_id/playlists", element: <PlaylistViewer /> },
```

### Step 3 — Verify + commit

- [ ] **Step 3a: Typecheck + visual verification**

Navigate to a station's playlists. Verify: playlist list, event table pagination, broadcast day calendar, M3U export download.

- [ ] **Step 3b: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): PlaylistViewer — playlist list, paginated events, broadcast calendar, M3U export"
```

---

## Task 7: Matcher Schemas/Hooks + ArtistPanel + TitlePanel

**Files:**
- Modify: `src/lib/schemas/matcher.ts`, `src/lib/schemas/matches.ts`
- Create: `src/api/matcher.ts`
- Create: `src/components/domain/matcher/ArtistPanel.tsx`
- Create: `src/components/domain/matcher/TitlePanel.tsx`
- Modify: `src/lib/schemas/index.ts`

### Step 1 — Matcher schemas

- [ ] **Step 1a: Replace `src/lib/schemas/matcher.ts`**

```typescript
import { z } from "zod";

export const QueueIdentitySchema = z.object({
  id: z.string().uuid(),
  original_title: z.string(),
  normalized_title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
});
export type QueueIdentity = z.infer<typeof QueueIdentitySchema>;

export const QueueArtistSchema = z.object({
  id: z.string().uuid(),
  original_name: z.string(),
  normalized_name: z.string(),
  match_status: z.string(),
  candidates: z.array(z.record(z.string(), z.unknown())).nullable(),
  identities: z.array(QueueIdentitySchema),
});
export type QueueArtist = z.infer<typeof QueueArtistSchema>;

export const MatchingQueueSchema = z.object({
  items: z.array(QueueArtistSchema),
  total: z.number(),
});
export type MatchingQueue = z.infer<typeof MatchingQueueSchema>;

export const ArtistResolutionSchema = z.object({
  match_status: z.enum(["MAN_MATCHED", "MAN_REJECTED"]),
  target_artist_id: z.string().nullable().optional(),
});
export type ArtistResolution = z.infer<typeof ArtistResolutionSchema>;

export const IdentityResolutionSchema = z.object({
  match_status: z.enum(["MAN_MATCHED", "MAN_REJECTED"]),
  library_file_id: z.string().uuid().nullable().optional(),
});
export type IdentityResolution = z.infer<typeof IdentityResolutionSchema>;

export const ResolveResultSchema = z.object({
  id: z.string().uuid(),
  match_status: z.string(),
});
export type ResolveResult = z.infer<typeof ResolveResultSchema>;

// Stub compat
export const MatcherQueueItemSchema = QueueArtistSchema;
```

- [ ] **Step 1b: Replace `src/lib/schemas/matches.ts`**

```typescript
import { z } from "zod";

export const MatchCandidateSchema = z.object({
  mbid: z.string(),
  name: z.string(),
  score: z.number(),
  disambiguation: z.string().optional(),
});
export type MatchCandidate = z.infer<typeof MatchCandidateSchema>;

export const MatchSchema = MatchCandidateSchema;
```

- [ ] **Step 1c: Update `src/lib/schemas/index.ts`**

Add: `export * from "./matcher";` and `export * from "./matches";`

### Step 2 — Matcher API hooks

- [ ] **Step 2a: Create `src/api/matcher.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type {
  MatchingQueue,
  ArtistResolution,
  IdentityResolution,
  ResolveResult,
} from "@/lib/schemas/matcher";

export function useMatchingQueue(limit = 50, offset = 0) {
  return useQuery<MatchingQueue>({
    queryKey: ["matching", "queue", limit, offset],
    queryFn: () =>
      apiFetch<MatchingQueue>(
        `/api/v1/matching/queue?limit=${limit}&offset=${offset}`,
      ),
  });
}

export function useResolveArtist() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      artistId,
      resolution,
    }: {
      artistId: string;
      resolution: ArtistResolution;
    }) =>
      apiFetch<ResolveResult>(`/api/v1/matching/artists/${artistId}/resolve`, {
        method: "POST",
        body: JSON.stringify(resolution),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["matching"] }),
  });
}

export function useResolveIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      identityId,
      resolution,
    }: {
      identityId: string;
      resolution: IdentityResolution;
    }) =>
      apiFetch<ResolveResult>(
        `/api/v1/matching/identities/${identityId}/resolve`,
        {
          method: "POST",
          body: JSON.stringify(resolution),
        },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["matching"] }),
  });
}

export function useRerunMatching() {
  return useMutation({
    mutationFn: () =>
      apiFetch<{ status: string; message: string }>("/api/v1/matching/run", {
        method: "POST",
      }),
  });
}
```

### Step 3 — ArtistPanel

- [ ] **Step 3a: Create `src/components/domain/matcher/ArtistPanel.tsx`**

```tsx
import { Check, X } from "lucide-react";

import { MatchStatusBadge } from "@/components/ui/Badge";
import { useResolveArtist } from "@/api/matcher";
import type { QueueArtist } from "@/lib/schemas/matcher";
import type { MatchCandidate } from "@/lib/schemas/matches";

interface ArtistPanelProps {
  artist: QueueArtist;
  onResolved: () => void;
}

export function ArtistPanel({ artist, onResolved }: ArtistPanelProps) {
  const resolveArtist = useResolveArtist();

  const candidates: MatchCandidate[] = (artist.candidates ?? []).map((c) => ({
    mbid: String(c.mbid ?? c.id ?? ""),
    name: String(c.name ?? ""),
    score: Number(c.score ?? 0),
    disambiguation: c.disambiguation ? String(c.disambiguation) : undefined,
  }));

  function handleAccept(mbid: string) {
    resolveArtist.mutate(
      {
        artistId: artist.id,
        resolution: { match_status: "MAN_MATCHED", target_artist_id: mbid },
      },
      { onSuccess: onResolved },
    );
  }

  function handleReject() {
    resolveArtist.mutate(
      {
        artistId: artist.id,
        resolution: { match_status: "MAN_REJECTED" },
      },
      { onSuccess: onResolved },
    );
  }

  return (
    <div className="bg-white border rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="font-semibold text-lg">{artist.original_name}</h3>
          <p className="text-sm text-gray-500">
            Normalized: {artist.normalized_name}
          </p>
        </div>
        <MatchStatusBadge status={artist.match_status} />
      </div>

      {candidates.length > 0 ? (
        <div className="space-y-2">
          <h4 className="text-sm font-medium text-gray-700">Candidates:</h4>
          {candidates.map((c) => (
            <div
              key={c.mbid}
              className="flex items-center justify-between p-3 border rounded-md hover:bg-gray-50"
            >
              <div>
                <span className="font-medium">{c.name}</span>
                {c.disambiguation && (
                  <span className="ml-2 text-xs text-gray-400">
                    ({c.disambiguation})
                  </span>
                )}
                <span className="ml-2 text-xs text-gray-500">
                  Score: {c.score}
                </span>
              </div>
              <button
                onClick={() => handleAccept(c.mbid)}
                disabled={resolveArtist.isPending}
                className="flex items-center gap-1 px-3 py-1 text-sm text-green-700 bg-green-50 hover:bg-green-100 rounded-md"
              >
                <Check className="h-3 w-3" /> Accept
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-gray-400">No candidates available.</p>
      )}

      <div className="mt-4 flex justify-end">
        <button
          onClick={handleReject}
          disabled={resolveArtist.isPending}
          className="flex items-center gap-1 px-3 py-1 text-sm text-red-700 bg-red-50 hover:bg-red-100 rounded-md"
        >
          <X className="h-3 w-3" /> Reject Artist
        </button>
      </div>
    </div>
  );
}
```

### Step 4 — TitlePanel

- [ ] **Step 4a: Create `src/components/domain/matcher/TitlePanel.tsx`**

```tsx
import { MatchStatusBadge } from "@/components/ui/Badge";
import { useResolveIdentity } from "@/api/matcher";
import type { QueueIdentity } from "@/lib/schemas/matcher";

interface TitlePanelProps {
  identities: QueueIdentity[];
  artistResolved: boolean;
  onFileSearch: (identityId: string) => void;
}

export function TitlePanel({
  identities,
  artistResolved,
  onFileSearch,
}: TitlePanelProps) {
  const resolveIdentity = useResolveIdentity();

  if (!artistResolved) {
    return (
      <div className="bg-gray-50 border rounded-lg p-4 text-sm text-gray-400">
        Resolve the artist first to unlock title matching.
      </div>
    );
  }

  if (!identities.length) {
    return (
      <div className="bg-gray-50 border rounded-lg p-4 text-sm text-gray-400">
        No identities for this artist.
      </div>
    );
  }

  function handleReject(identityId: string) {
    resolveIdentity.mutate({
      identityId,
      resolution: { match_status: "MAN_REJECTED" },
    });
  }

  return (
    <div className="space-y-2">
      <h4 className="text-sm font-medium text-gray-700">Titles:</h4>
      {identities.map((identity) => (
        <div
          key={identity.id}
          className="flex items-center justify-between p-3 border rounded-md bg-white"
        >
          <div>
            <span className="font-medium">{identity.original_title}</span>
            <div className="flex items-center gap-2 mt-1">
              <MatchStatusBadge status={identity.match_status} />
              {identity.match_tier && (
                <span className="text-xs text-gray-400">
                  {identity.match_tier}
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onFileSearch(identity.id)}
              className="px-3 py-1 text-sm text-blue-700 bg-blue-50 hover:bg-blue-100 rounded-md"
            >
              Find File
            </button>
            <button
              onClick={() => handleReject(identity.id)}
              disabled={resolveIdentity.isPending}
              className="px-3 py-1 text-sm text-red-700 bg-red-50 hover:bg-red-100 rounded-md"
            >
              Reject
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
```

### Step 5 — Verify + commit

- [ ] **Step 5a: Typecheck**

Run: `cd frontend && npx tsc --noEmit`

- [ ] **Step 5b: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): matcher schemas/hooks, ArtistPanel, TitlePanel"
```

---

## Task 8: SearchSlideOver

**Files:**
- Create: `src/components/domain/matcher/SearchSlideOver.tsx`

### Step 1 — SearchSlideOver

- [ ] **Step 1a: Create `src/components/domain/matcher/SearchSlideOver.tsx`**

```tsx
import { useState, useEffect, useCallback } from "react";
import { X, Search } from "lucide-react";

import { apiFetch } from "@/api/client";
import { Spinner } from "@/components/ui/Spinner";
import { cn } from "@/lib/utils";
import type { ArtistSummary, PaginatedArtists } from "@/lib/schemas/library";
import type { FileInfo } from "@/lib/schemas/works";

type Mode = "artist" | "file";

interface SearchSlideOverProps {
  open: boolean;
  onClose: () => void;
  mode: Mode;
  restrictArtistMbid?: string;
  onSelectArtist?: (mbid: string, name: string) => void;
  onSelectFile?: (fileId: string) => void;
}

// Note: These types need to match the API response shapes.
// We import from schemas that are populated in later tasks.
// For now, define inline if schemas aren't populated yet.
interface SearchArtist {
  id: string;
  name: string;
  sort_name: string;
  disambiguation: string | null;
}

interface SearchFile {
  id: string;
  file_path: string;
  format: string;
  track_title: string | null;
  release_title: string | null;
  duration_ms: number | null;
}

export function SearchSlideOver({
  open,
  onClose,
  mode,
  restrictArtistMbid,
  onSelectArtist,
  onSelectFile,
}: SearchSlideOverProps) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchArtist[] | SearchFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [restrictToArtist, setRestrictToArtist] = useState(true);

  const search = useCallback(async (q: string) => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    setLoading(true);
    try {
      if (mode === "artist") {
        const data = await apiFetch<{ items: SearchArtist[]; total: number }>(
          `/api/v1/library/artists?search=${encodeURIComponent(q)}&limit=20`,
        );
        setResults(data.items);
      } else {
        // File search — query library files by artist
        const params = new URLSearchParams({ search: q, limit: "20" });
        if (restrictToArtist && restrictArtistMbid) {
          params.set("artist_mbid", restrictArtistMbid);
        }
        const data = await apiFetch<{ items: SearchFile[]; total: number }>(
          `/api/v1/library/artists?${params}`,
        );
        setResults(data.items);
      }
    } catch {
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, [mode, restrictArtistMbid, restrictToArtist]);

  // Debounced search
  useEffect(() => {
    const timer = setTimeout(() => search(query), 300);
    return () => clearTimeout(timer);
  }, [query, search]);

  // Reset on open
  useEffect(() => {
    if (open) {
      setQuery("");
      setResults([]);
    }
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-y-0 right-0 w-96 bg-white shadow-xl border-l z-50 flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b">
        <h3 className="font-semibold">
          Search {mode === "artist" ? "Artists" : "Files"}
        </h3>
        <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="px-4 py-3 border-b space-y-2">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${mode === "artist" ? "artist names" : "files"}...`}
            autoFocus
            className="w-full pl-9 pr-3 py-2 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {mode === "file" && restrictArtistMbid && (
          <label className="flex items-center gap-2 text-xs text-gray-600">
            <input
              type="checkbox"
              checked={restrictToArtist}
              onChange={(e) => setRestrictToArtist(e.target.checked)}
              className="rounded"
            />
            Restrict to confirmed artist
          </label>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        ) : results.length === 0 ? (
          <div className="text-center py-8 text-sm text-gray-400">
            {query ? "No results found" : "Type to search"}
          </div>
        ) : mode === "artist" ? (
          <div className="divide-y">
            {(results as SearchArtist[]).map((a) => (
              <button
                key={a.id}
                onClick={() => onSelectArtist?.(a.id, a.name)}
                className="w-full text-left px-4 py-3 hover:bg-gray-50 text-sm"
              >
                <div className="font-medium">{a.name}</div>
                {a.disambiguation && (
                  <div className="text-xs text-gray-400">{a.disambiguation}</div>
                )}
              </button>
            ))}
          </div>
        ) : (
          <div className="divide-y">
            {(results as SearchFile[]).map((f) => (
              <button
                key={f.id}
                onClick={() => onSelectFile?.(f.id)}
                className="w-full text-left px-4 py-3 hover:bg-gray-50 text-sm"
              >
                <div className="font-medium">{f.track_title ?? f.file_path}</div>
                <div className="text-xs text-gray-400 truncate">{f.file_path}</div>
                <div className="text-xs text-gray-400">
                  {f.format} {f.release_title && `— ${f.release_title}`}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
```

### Step 2 — Verify + commit

- [ ] **Step 2a: Typecheck + commit**

```bash
cd frontend && npx tsc --noEmit
git add frontend/src/
git commit -m "feat(frontend): SearchSlideOver — artist and file search with debounce"
```

---

## Task 9: MatcherBrowser Page

**Files:**
- Create: `src/pages/matcher/MatcherBrowser.tsx`
- Modify: `src/main.tsx`

### Step 1 — MatcherBrowser

- [ ] **Step 1a: Create `src/pages/matcher/MatcherBrowser.tsx`**

```tsx
import { useState } from "react";
import { RefreshCw } from "lucide-react";

import { useMatchingQueue, useRerunMatching, useResolveIdentity } from "@/api/matcher";
import { PageHeader } from "@/components/ui/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { Spinner } from "@/components/ui/Spinner";
import { ArtistPanel } from "@/components/domain/matcher/ArtistPanel";
import { TitlePanel } from "@/components/domain/matcher/TitlePanel";
import { SearchSlideOver } from "@/components/domain/matcher/SearchSlideOver";
import type { QueueArtist } from "@/lib/schemas/matcher";

export default function MatcherBrowser() {
  const { data, isLoading, refetch } = useMatchingQueue();
  const rerun = useRerunMatching();
  const resolveIdentity = useResolveIdentity();

  const [selectedArtist, setSelectedArtist] = useState<QueueArtist | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchIdentityId, setSearchIdentityId] = useState<string | null>(null);

  function handleArtistResolved() {
    setSelectedArtist(null);
    refetch();
  }

  function handleFileSearch(identityId: string) {
    setSearchIdentityId(identityId);
    setSearchOpen(true);
  }

  function handleFileSelected(fileId: string) {
    if (!searchIdentityId) return;
    resolveIdentity.mutate(
      {
        identityId: searchIdentityId,
        resolution: { match_status: "MAN_MATCHED", library_file_id: fileId },
      },
      {
        onSuccess: () => {
          setSearchOpen(false);
          setSearchIdentityId(null);
          refetch();
        },
      },
    );
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  const artists = data?.items ?? [];
  const isArtistResolved = selectedArtist
    ? ["MAN_MATCHED", "AUTO_MATCHED"].includes(selectedArtist.match_status)
    : false;

  return (
    <>
      <PageHeader
        title="Matcher"
        description={`${data?.total ?? 0} artist(s) need review`}
        actions={
          <button
            onClick={() => rerun.mutate(undefined, { onSuccess: () => refetch() })}
            disabled={rerun.isPending}
            className="flex items-center gap-2 px-3 py-2 text-sm border rounded-md hover:bg-gray-50"
          >
            <RefreshCw className={`h-4 w-4 ${rerun.isPending ? "animate-spin" : ""}`} />
            Re-run Matching
          </button>
        }
      />

      {artists.length === 0 ? (
        <EmptyState
          title="Queue is empty"
          description="All artists have been resolved, or no playlists have been imported."
        />
      ) : (
        <div className="flex gap-6">
          {/* Left: artist list */}
          <div className="w-80 shrink-0 space-y-2 max-h-[calc(100vh-12rem)] overflow-y-auto">
            {artists.map((artist) => (
              <button
                key={artist.id}
                onClick={() => setSelectedArtist(artist)}
                className={`w-full text-left p-3 border rounded-lg text-sm ${
                  selectedArtist?.id === artist.id ? "border-blue-500 bg-blue-50" : "hover:bg-gray-50"
                }`}
              >
                <div className="font-medium">{artist.original_name}</div>
                <div className="text-xs text-gray-500">
                  {artist.identities.length} title(s)
                </div>
              </button>
            ))}
          </div>

          {/* Right: detail panel */}
          <div className="flex-1 space-y-4">
            {selectedArtist ? (
              <>
                <ArtistPanel
                  artist={selectedArtist}
                  onResolved={handleArtistResolved}
                />
                <TitlePanel
                  identities={selectedArtist.identities}
                  artistResolved={isArtistResolved}
                  onFileSearch={handleFileSearch}
                />
              </>
            ) : (
              <div className="text-center py-12 text-gray-400 text-sm">
                Select an artist from the list to review.
              </div>
            )}
          </div>
        </div>
      )}

      <SearchSlideOver
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        mode="file"
        onSelectFile={handleFileSelected}
      />
    </>
  );
}
```

### Step 2 — Wire into router + commit

- [ ] **Step 2a: Update `src/main.tsx`**

```typescript
import MatcherBrowser from "@/pages/matcher/MatcherBrowser";
// Replace matcher index placeholder
```

- [ ] **Step 2b: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): MatcherBrowser — artist-first resolution workflow"
```

---

## Task 10: ScannerActions Page

**Files:**
- Create: `src/pages/matcher/ScannerActions.tsx`
- Modify: `src/main.tsx`

### Step 1 — ScannerActions

- [ ] **Step 1a: Create `src/pages/matcher/ScannerActions.tsx`**

```tsx
import { useState } from "react";
import { FolderSearch, Upload } from "lucide-react";

import { apiFetch } from "@/api/client";
import { PageHeader } from "@/components/ui/PageHeader";

export default function ScannerActions() {
  const [scanPath, setScanPath] = useState("");
  const [scanning, setScanning] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function handleScan() {
    if (!scanPath.trim()) return;
    setScanning(true);
    setMessage(null);
    try {
      await apiFetch("/api/v1/library/scan", {
        method: "POST",
        body: JSON.stringify({ root_path: scanPath.trim() }),
      });
      setMessage("Library scan started. Check the progress bar.");
    } catch (err) {
      setMessage(`Error: ${err instanceof Error ? err.message : "Unknown"}`);
    } finally {
      setScanning(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Scanner"
        description="Scan your music library and import playlists"
      />

      <div className="max-w-xl space-y-6">
        {/* Library scan */}
        <div className="bg-white border rounded-lg p-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="p-2 bg-blue-50 rounded-lg">
              <FolderSearch className="h-5 w-5 text-blue-600" />
            </div>
            <div>
              <h3 className="font-semibold">Scan Library</h3>
              <p className="text-sm text-gray-500">
                Scan a directory for audio files (FLAC, MP3, AAC, OGG).
              </p>
            </div>
          </div>

          <div className="flex gap-2">
            <input
              type="text"
              value={scanPath}
              onChange={(e) => setScanPath(e.target.value)}
              placeholder="e.g. D:\Music"
              className="flex-1 rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <button
              onClick={handleScan}
              disabled={scanning || !scanPath.trim()}
              className="px-4 py-2 text-sm text-white bg-blue-600 hover:bg-blue-700 rounded-md disabled:opacity-50"
            >
              {scanning ? "Starting..." : "Scan"}
            </button>
          </div>
        </div>

        {message && (
          <div className={`text-sm p-3 rounded-md ${
            message.startsWith("Error") ? "bg-red-50 text-red-700" : "bg-green-50 text-green-700"
          }`}>
            {message}
          </div>
        )}
      </div>
    </>
  );
}
```

### Step 2 — Wire + commit

- [ ] **Step 2a: Update `src/main.tsx`, commit**

```typescript
import ScannerActions from "@/pages/matcher/ScannerActions";
// Replace scanner placeholder
```

```bash
git add frontend/src/
git commit -m "feat(frontend): ScannerActions — library scan trigger"
```

---

## Task 11: Library Schemas/Hooks + LibraryStatus Page

**Files:**
- Modify: `src/lib/schemas/library.ts`
- Create: `src/api/library.ts`
- Create: `src/pages/library/LibraryStatus.tsx`
- Modify: `src/main.tsx`, `src/lib/schemas/index.ts`

### Step 1 — Library schemas

- [ ] **Step 1a: Replace `src/lib/schemas/library.ts`**

```typescript
import { z } from "zod";

export const LibraryStatusSchema = z.object({
  total_files: z.number(),
  quarantine_count: z.number(),
  by_format: z.record(z.string(), z.number()),
  by_enrichment: z.record(z.string(), z.number()),
});
export type LibraryStatus = z.infer<typeof LibraryStatusSchema>;

export const LibraryFileSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
  format: z.string(),
  bitrate: z.number().nullable(),
  duration_ms: z.number().nullable(),
  track_title: z.string().nullable(),
  release_title: z.string().nullable(),
  enrichment_status: z.string(),
});
export type LibraryFileInfo = z.infer<typeof LibraryFileSchema>;
```

- [ ] **Step 1b: Update index.ts**

Add: `export * from "./library";`

### Step 2 — Library API hooks

- [ ] **Step 2a: Create `src/api/library.ts`**

```typescript
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type { LibraryStatus } from "@/lib/schemas/library";

export function useLibraryStatus() {
  return useQuery<LibraryStatus>({
    queryKey: ["library", "status"],
    queryFn: () => apiFetch<LibraryStatus>("/api/v1/library/status"),
  });
}
```

### Step 3 — LibraryStatus page

- [ ] **Step 3a: Create `src/pages/library/LibraryStatus.tsx`**

```tsx
import { Link } from "react-router-dom";
import { Database, AlertTriangle, Users } from "lucide-react";

import { useLibraryStatus } from "@/api/library";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";

export default function LibraryStatus() {
  const { data, isLoading } = useLibraryStatus();

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  if (!data) return null;

  const enriched = data.by_enrichment.enriched ?? 0;
  const enrichPct = data.total_files > 0 ? Math.round((enriched / data.total_files) * 100) : 0;

  return (
    <>
      <PageHeader
        title="Library"
        description={`${data.total_files.toLocaleString()} files indexed`}
        actions={
          <Link
            to="/library/artists"
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-md"
          >
            <Users className="h-4 w-4" /> Browse Artists
          </Link>
        }
      />

      <div className="grid gap-4 sm:grid-cols-3 mb-8">
        <div className="bg-white border rounded-lg p-5">
          <div className="flex items-center gap-3">
            <Database className="h-8 w-8 text-blue-500" />
            <div>
              <div className="text-2xl font-bold">{data.total_files.toLocaleString()}</div>
              <div className="text-sm text-gray-500">Total Files</div>
            </div>
          </div>
        </div>

        <div className="bg-white border rounded-lg p-5">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 flex items-center justify-center text-lg font-bold text-green-600">
              {enrichPct}%
            </div>
            <div>
              <div className="text-2xl font-bold">{enriched.toLocaleString()}</div>
              <div className="text-sm text-gray-500">Enriched</div>
            </div>
          </div>
        </div>

        {data.quarantine_count > 0 && (
          <div className="bg-white border rounded-lg p-5">
            <div className="flex items-center gap-3">
              <AlertTriangle className="h-8 w-8 text-amber-500" />
              <div>
                <div className="text-2xl font-bold">{data.quarantine_count}</div>
                <div className="text-sm text-gray-500">Quarantined</div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Format breakdown */}
      <div className="bg-white border rounded-lg p-5 mb-6">
        <h3 className="font-semibold mb-3">By Format</h3>
        <div className="flex flex-wrap gap-3">
          {Object.entries(data.by_format)
            .sort(([, a], [, b]) => b - a)
            .map(([fmt, count]) => (
              <div key={fmt} className="px-3 py-2 bg-gray-50 rounded-md text-sm">
                <span className="font-medium uppercase">{fmt}</span>
                <span className="ml-2 text-gray-500">{count.toLocaleString()}</span>
              </div>
            ))}
        </div>
      </div>

      {/* Enrichment breakdown */}
      <div className="bg-white border rounded-lg p-5">
        <h3 className="font-semibold mb-3">By Enrichment Status</h3>
        <div className="flex flex-wrap gap-3">
          {Object.entries(data.by_enrichment)
            .sort(([, a], [, b]) => b - a)
            .map(([status, count]) => (
              <div key={status} className="px-3 py-2 bg-gray-50 rounded-md text-sm">
                <span className="font-medium capitalize">{status}</span>
                <span className="ml-2 text-gray-500">{count.toLocaleString()}</span>
              </div>
            ))}
        </div>
      </div>
    </>
  );
}
```

### Step 4 — Wire + commit

- [ ] **Step 4a: Update router, commit**

```typescript
import LibraryStatus from "@/pages/library/LibraryStatus";
// Replace library index placeholder
```

```bash
git add frontend/src/
git commit -m "feat(frontend): LibraryStatus page — file counts, format/enrichment breakdown"
```

---

## Task 12: ArtistBrowser (Virtual Scrolling)

**Files:**
- Modify: `src/lib/schemas/artists.ts`
- Create: `src/api/artists.ts`
- Create: `src/pages/library/ArtistBrowser.tsx`
- Modify: `src/main.tsx`, `src/lib/schemas/index.ts`

### Step 1 — Artists schemas + hooks

- [ ] **Step 1a: Replace `src/lib/schemas/artists.ts`**

```typescript
import { z } from "zod";

export const ArtistSummarySchema = z.object({
  id: z.string(),
  name: z.string(),
  sort_name: z.string(),
  disambiguation: z.string().nullable(),
  work_count: z.number(),
  file_count: z.number(),
});
export type ArtistSummary = z.infer<typeof ArtistSummarySchema>;

export const PaginatedArtistsSchema = z.object({
  items: z.array(ArtistSummarySchema),
  total: z.number(),
});
export type PaginatedArtists = z.infer<typeof PaginatedArtistsSchema>;

export const WorkSummarySchema = z.object({
  id: z.string(),
  title: z.string(),
  recording_count: z.number(),
  has_master: z.boolean(),
});
export type WorkSummary = z.infer<typeof WorkSummarySchema>;

export const ArtistDetailSchema = z.object({
  id: z.string(),
  name: z.string(),
  sort_name: z.string(),
  disambiguation: z.string().nullable(),
  works: z.array(WorkSummarySchema),
});
export type ArtistDetail = z.infer<typeof ArtistDetailSchema>;

// Stub compat
export const ArtistSchema = ArtistSummarySchema;
export const ArtistSearchResultSchema = ArtistSummarySchema;
```

- [ ] **Step 1b: Update index.ts**

Add: `export * from "./artists";`

- [ ] **Step 1c: Create `src/api/artists.ts`**

```typescript
import { useQuery, useInfiniteQuery } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type { PaginatedArtists, ArtistDetail } from "@/lib/schemas/artists";

const PAGE_SIZE = 50;

export function useArtistsInfinite(search?: string) {
  return useInfiniteQuery<PaginatedArtists>({
    queryKey: ["library", "artists", search],
    queryFn: ({ pageParam = 0 }) => {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(pageParam),
      });
      if (search) params.set("search", search);
      return apiFetch<PaginatedArtists>(`/api/v1/library/artists?${params}`);
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, p) => n + p.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
}

export function useArtistDetail(artistId: string | undefined) {
  return useQuery<ArtistDetail>({
    queryKey: ["library", "artists", artistId],
    queryFn: () => apiFetch<ArtistDetail>(`/api/v1/library/artists/${artistId}`),
    enabled: !!artistId,
  });
}
```

### Step 2 — ArtistBrowser with virtual scroll

- [ ] **Step 2a: Create `src/pages/library/ArtistBrowser.tsx`**

```tsx
import { useState, useRef, useEffect, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Search, ArrowLeft } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";

import { useArtistsInfinite } from "@/api/artists";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";

const ROW_HEIGHT = 56;

export default function ArtistBrowser() {
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const parentRef = useRef<HTMLDivElement>(null);

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading } =
    useArtistsInfinite(debouncedSearch || undefined);

  const allArtists = useMemo(
    () => data?.pages.flatMap((p) => p.items) ?? [],
    [data],
  );
  const total = data?.pages[0]?.total ?? 0;

  const virtualizer = useVirtualizer({
    count: allArtists.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 5,
  });

  // Load more when near bottom
  useEffect(() => {
    const items = virtualizer.getVirtualItems();
    const lastItem = items[items.length - 1];
    if (!lastItem) return;
    if (
      lastItem.index >= allArtists.length - 5 &&
      hasNextPage &&
      !isFetchingNextPage
    ) {
      fetchNextPage();
    }
  }, [virtualizer.getVirtualItems(), hasNextPage, isFetchingNextPage, allArtists.length, fetchNextPage]);

  return (
    <>
      <div className="mb-4">
        <button
          onClick={() => navigate("/library")}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" /> Back to library
        </button>
      </div>

      <PageHeader
        title="Artists"
        description={`${total.toLocaleString()} artists in library`}
      />

      {/* Search */}
      <div className="mb-4 relative max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search artists..."
          className="w-full pl-9 pr-3 py-2 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      </div>

      {isLoading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-8 w-8 text-gray-400" />
        </div>
      ) : (
        <div
          ref={parentRef}
          className="h-[calc(100vh-16rem)] overflow-auto border rounded-lg bg-white"
        >
          <div
            style={{
              height: `${virtualizer.getTotalSize()}px`,
              width: "100%",
              position: "relative",
            }}
          >
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const artist = allArtists[virtualRow.index];
              return (
                <Link
                  key={artist.id}
                  to={`/library/artists/${artist.id}`}
                  className="absolute inset-x-0 flex items-center px-4 hover:bg-gray-50 border-b"
                  style={{
                    top: 0,
                    height: `${virtualRow.size}px`,
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-sm truncate">
                      {artist.name}
                    </div>
                    {artist.disambiguation && (
                      <div className="text-xs text-gray-400 truncate">
                        {artist.disambiguation}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-4 text-xs text-gray-500 shrink-0">
                    <span>{artist.work_count} works</span>
                    <span>{artist.file_count} files</span>
                  </div>
                </Link>
              );
            })}
          </div>
          {isFetchingNextPage && (
            <div className="flex justify-center py-2">
              <Spinner />
            </div>
          )}
        </div>
      )}
    </>
  );
}
```

### Step 3 — Wire + commit

- [ ] **Step 3a: Update router, commit**

```typescript
import ArtistBrowser from "@/pages/library/ArtistBrowser";
// Replace: { path: "artists", element: <Placeholder name="ArtistBrowser" /> },
```

```bash
git add frontend/src/
git commit -m "feat(frontend): ArtistBrowser — virtual scrolling with infinite query and debounced search"
```

---

## Task 13: ArtistDetail Section A (Primary Works)

**Files:**
- Create: `src/pages/library/ArtistDetail.tsx`
- Modify: `src/main.tsx`

### Step 1 — ArtistDetail

- [ ] **Step 1a: Create `src/pages/library/ArtistDetail.tsx`**

```tsx
import { useParams, useNavigate, Link } from "react-router-dom";
import { ArrowLeft, Crown, Music } from "lucide-react";

import { useArtistDetail } from "@/api/artists";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { EmptyState } from "@/components/ui/EmptyState";
import { Badge } from "@/components/ui/Badge";

export default function ArtistDetail() {
  const { artist_id } = useParams<{ artist_id: string }>();
  const navigate = useNavigate();
  const { data: artist, isLoading } = useArtistDetail(artist_id);

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  if (!artist) {
    return <div className="text-gray-500">Artist not found</div>;
  }

  return (
    <>
      <div className="mb-4">
        <button
          onClick={() => navigate("/library/artists")}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" /> Back to artists
        </button>
      </div>

      <PageHeader
        title={artist.name}
        description={
          [artist.sort_name !== artist.name ? artist.sort_name : null, artist.disambiguation]
            .filter(Boolean)
            .join(" — ") || undefined
        }
      />

      {/* Works list */}
      {artist.works.length === 0 ? (
        <EmptyState title="No works" description="This artist has no cataloged works." />
      ) : (
        <div className="space-y-2">
          <h2 className="text-lg font-semibold mb-3">
            Works ({artist.works.length})
          </h2>
          {artist.works.map((work) => (
            <Link
              key={work.id}
              to={`/library/artists/${artist.id}/works/${work.id}`}
              className="flex items-center justify-between p-4 bg-white border rounded-lg hover:shadow-sm transition-shadow"
            >
              <div className="flex items-center gap-3">
                <Music className="h-4 w-4 text-gray-400" />
                <div>
                  <div className="font-medium">{work.title}</div>
                  <div className="text-xs text-gray-500">
                    {work.recording_count} recording(s)
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {work.has_master && (
                  <Crown className="h-4 w-4 text-amber-500" title="Has master" />
                )}
                <Badge>{work.recording_count} rec</Badge>
              </div>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}
```

### Step 2 — Wire + commit

- [ ] **Step 2a: Update router, commit**

```typescript
import ArtistDetail from "@/pages/library/ArtistDetail";
// Replace artist detail placeholder
```

```bash
git add frontend/src/
git commit -m "feat(frontend): ArtistDetail page — works list with master indicators"
```

---

## Task 14: ArtistDetail Section B + FeaturedReleasesSection

This task is about enhancing ArtistDetail. In the current design, the "Section B" and FeaturedReleasesSection are additional data displays on the artist page. Since the API returns works grouped under the artist, the featured releases section shows library files where `artist_mbid != album_artist_mbid` (featured appearances).

**Files:**
- Create: `src/components/domain/library/FeaturedReleasesSection.tsx`
- Modify: `src/pages/library/ArtistDetail.tsx`

### Step 1 — FeaturedReleasesSection

- [ ] **Step 1a: Create `src/components/domain/library/FeaturedReleasesSection.tsx`**

```tsx
import { Disc } from "lucide-react";

// This section would display releases where this artist appears as a featured artist.
// Since we don't have a dedicated API endpoint for featured releases yet,
// this is a placeholder that can be populated when the API supports it.

export function FeaturedReleasesSection({ artistId }: { artistId: string }) {
  // Future: fetch library files where artist_mbid = artistId AND album_artist_mbid != artistId
  return (
    <div className="mt-8">
      <h2 className="text-lg font-semibold mb-3 flex items-center gap-2">
        <Disc className="h-5 w-5 text-gray-400" />
        Featured Appearances
      </h2>
      <div className="text-sm text-gray-400 bg-gray-50 border rounded-lg p-4">
        Featured release detection requires album_artist_mbid data from library enrichment.
        Appearances will show here once enrichment is complete.
      </div>
    </div>
  );
}
```

### Step 2 — Add to ArtistDetail

- [ ] **Step 2a: Modify `src/pages/library/ArtistDetail.tsx`**

Add after the works list:

```tsx
import { FeaturedReleasesSection } from "@/components/domain/library/FeaturedReleasesSection";

// At the bottom of the component, after the works list:
<FeaturedReleasesSection artistId={artist.id} />
```

### Step 3 — Commit

- [ ] **Step 3a: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): FeaturedReleasesSection placeholder on ArtistDetail"
```

---

## Task 15: Works Schemas/Hooks + WorkFilesTable + FormatOverridePanel

**Files:**
- Modify: `src/lib/schemas/works.ts`
- Create: `src/api/works.ts`
- Create: `src/components/domain/works/WorkFilesTable.tsx`
- Create: `src/components/domain/works/FormatOverridePanel.tsx`
- Modify: `src/lib/schemas/index.ts`

### Step 1 — Works schemas

- [ ] **Step 1a: Replace `src/lib/schemas/works.ts`**

```typescript
import { z } from "zod";

export const FileInfoSchema = z.object({
  id: z.string().uuid(),
  file_path: z.string(),
  format: z.string(),
  bitrate: z.number().nullable(),
  duration_ms: z.number().nullable(),
  track_title: z.string().nullable(),
  release_title: z.string().nullable(),
  enrichment_status: z.string(),
});
export type FileInfo = z.infer<typeof FileInfoSchema>;

export const RecordingDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  version_type: z.string(),
  duration_ms: z.number().nullable(),
  files: z.array(FileInfoSchema),
});
export type RecordingDetail = z.infer<typeof RecordingDetailSchema>;

export const SongMasterInfoSchema = z.object({
  id: z.string().uuid(),
  preferred_file_id: z.string().uuid(),
  selection_method: z.string(),
  score: z.number().nullable(),
  updated_at: z.string(),
});
export type SongMasterInfo = z.infer<typeof SongMasterInfoSchema>;

export const FormatOverrideInfoSchema = z.object({
  id: z.string().uuid(),
  format_name: z.string(),
  preferred_file_id: z.string().uuid(),
  notes: z.string().nullable(),
  created_at: z.string(),
});
export type FormatOverrideInfo = z.infer<typeof FormatOverrideInfoSchema>;

export const WorkDetailSchema = z.object({
  id: z.string(),
  title: z.string(),
  artist_id: z.string(),
  recordings: z.array(RecordingDetailSchema),
  song_master: SongMasterInfoSchema.nullable(),
  format_overrides: z.array(FormatOverrideInfoSchema),
});
export type WorkDetail = z.infer<typeof WorkDetailSchema>;

// Stub compat
export const WorkSchema = WorkDetailSchema;
export const WorkFilesTableRowSchema = FileInfoSchema;
```

- [ ] **Step 1b: Update index.ts**

Add: `export * from "./works";`

### Step 2 — Works API hooks

- [ ] **Step 2a: Create `src/api/works.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type { WorkDetail, FormatOverrideInfo, SongMasterInfo } from "@/lib/schemas/works";

export function useWorkDetail(workId: string | undefined) {
  return useQuery<WorkDetail>({
    queryKey: ["library", "works", workId],
    queryFn: () => apiFetch<WorkDetail>(`/api/v1/library/works/${workId}`),
    enabled: !!workId,
  });
}

export function useSetMaster(workId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (preferredFileId: string) =>
      apiFetch<SongMasterInfo>(`/api/v1/library/works/${workId}/master`, {
        method: "PUT",
        body: JSON.stringify({ preferred_file_id: preferredFileId }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["library", "works", workId] });
      qc.invalidateQueries({ queryKey: ["library", "artists"] });
    },
  });
}

export function useRevertMaster(workId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<void>(`/api/v1/library/works/${workId}/master`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["library", "works", workId] });
      qc.invalidateQueries({ queryKey: ["library", "artists"] });
    },
  });
}

export function useCreateFormatOverride(workId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { format_name: string; preferred_file_id: string; notes?: string }) =>
      apiFetch<FormatOverrideInfo>(
        `/api/v1/library/works/${workId}/format-overrides`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["library", "works", workId] }),
  });
}

export function useDeleteFormatOverride(workId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (overrideId: string) =>
      apiFetch<void>(
        `/api/v1/library/works/${workId}/format-overrides/${overrideId}`,
        { method: "DELETE" },
      ),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["library", "works", workId] }),
  });
}
```

### Step 3 — WorkFilesTable

- [ ] **Step 3a: Create `src/components/domain/works/WorkFilesTable.tsx`**

```tsx
import { Crown, CrownIcon } from "lucide-react";

import { formatDuration, cn } from "@/lib/utils";
import type { RecordingDetail, FileInfo } from "@/lib/schemas/works";

interface WorkFilesTableProps {
  recordings: RecordingDetail[];
  masterFileId: string | null;
  masterMethod: string | null;
  onSetMaster: (fileId: string) => void;
}

export function WorkFilesTable({
  recordings,
  masterFileId,
  masterMethod,
  onSetMaster,
}: WorkFilesTableProps) {
  return (
    <div className="space-y-4">
      {recordings.map((rec) => (
        <div key={rec.id} className="border rounded-lg overflow-hidden">
          <div className="bg-gray-50 px-4 py-2 text-sm font-medium flex items-center gap-2">
            <span>{rec.title}</span>
            <span className="text-xs text-gray-400 uppercase">
              {rec.version_type}
            </span>
            {rec.duration_ms && (
              <span className="text-xs text-gray-400">
                {formatDuration(rec.duration_ms)}
              </span>
            )}
          </div>
          {rec.files.length === 0 ? (
            <div className="px-4 py-3 text-sm text-gray-400">No files</div>
          ) : (
            <table className="min-w-full divide-y text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500">
                  <th className="px-4 py-2 w-8"></th>
                  <th className="px-4 py-2">File</th>
                  <th className="px-4 py-2">Format</th>
                  <th className="px-4 py-2">Bitrate</th>
                  <th className="px-4 py-2">Duration</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {rec.files.map((file) => {
                  const isMaster = file.id === masterFileId;
                  return (
                    <tr
                      key={file.id}
                      className={cn("hover:bg-gray-50", isMaster && "bg-amber-50")}
                    >
                      <td className="px-4 py-2">
                        <button
                          onClick={() => onSetMaster(file.id)}
                          title={isMaster ? "Current master" : "Set as master"}
                          className="text-gray-300 hover:text-amber-500"
                        >
                          <Crown
                            className={cn(
                              "h-4 w-4",
                              isMaster && masterMethod === "manual" && "text-amber-500 fill-amber-500",
                              isMaster && masterMethod === "auto" && "text-amber-500",
                            )}
                          />
                        </button>
                      </td>
                      <td className="px-4 py-2">
                        <div className="truncate max-w-xs" title={file.file_path}>
                          {file.track_title ?? file.file_path.split(/[/\\]/).pop()}
                        </div>
                        {file.release_title && (
                          <div className="text-xs text-gray-400">{file.release_title}</div>
                        )}
                      </td>
                      <td className="px-4 py-2 uppercase text-gray-500">{file.format}</td>
                      <td className="px-4 py-2 text-gray-500">
                        {file.bitrate ? `${file.bitrate} kbps` : "—"}
                      </td>
                      <td className="px-4 py-2 text-gray-500">
                        {formatDuration(file.duration_ms)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </div>
  );
}
```

### Step 4 — FormatOverridePanel

- [ ] **Step 4a: Create `src/components/domain/works/FormatOverridePanel.tsx`**

```tsx
import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import { useCreateFormatOverride, useDeleteFormatOverride } from "@/api/works";
import type { FormatOverrideInfo, RecordingDetail } from "@/lib/schemas/works";

interface FormatOverridePanelProps {
  workId: string;
  overrides: FormatOverrideInfo[];
  recordings: RecordingDetail[];
}

export function FormatOverridePanel({
  workId,
  overrides,
  recordings,
}: FormatOverridePanelProps) {
  const createOverride = useCreateFormatOverride(workId);
  const deleteOverride = useDeleteFormatOverride(workId);
  const [showForm, setShowForm] = useState(false);
  const [formatName, setFormatName] = useState("");
  const [fileId, setFileId] = useState("");
  const [notes, setNotes] = useState("");

  const allFiles = recordings.flatMap((r) => r.files);

  function handleCreate() {
    if (!formatName.trim() || !fileId) return;
    createOverride.mutate(
      {
        format_name: formatName.trim(),
        preferred_file_id: fileId,
        notes: notes.trim() || undefined,
      },
      {
        onSuccess: () => {
          setShowForm(false);
          setFormatName("");
          setFileId("");
          setNotes("");
        },
      },
    );
  }

  return (
    <div className="border rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-sm">Format Overrides</h3>
        <button
          onClick={() => setShowForm(true)}
          className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700"
        >
          <Plus className="h-3 w-3" /> Add Override
        </button>
      </div>

      {overrides.length === 0 && !showForm ? (
        <p className="text-sm text-gray-400">
          No format-specific overrides. The song master will be used for all formats.
        </p>
      ) : (
        <div className="space-y-2">
          {overrides.map((o) => (
            <div
              key={o.id}
              className="flex items-center justify-between p-2 bg-gray-50 rounded-md text-sm"
            >
              <div>
                <span className="font-medium">{o.format_name}</span>
                {o.notes && <span className="ml-2 text-xs text-gray-400">{o.notes}</span>}
              </div>
              <button
                onClick={() => deleteOverride.mutate(o.id)}
                className="p-1 text-gray-400 hover:text-red-500"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <div className="mt-3 p-3 border rounded-md space-y-2">
          <input
            type="text"
            value={formatName}
            onChange={(e) => setFormatName(e.target.value)}
            placeholder="Format name (e.g. CHR)"
            className="w-full text-sm border rounded px-2 py-1"
          />
          <select
            value={fileId}
            onChange={(e) => setFileId(e.target.value)}
            className="w-full text-sm border rounded px-2 py-1"
          >
            <option value="">Select a file...</option>
            {allFiles.map((f) => (
              <option key={f.id} value={f.id}>
                {f.track_title ?? f.file_path.split(/[/\\]/).pop()} ({f.format})
              </option>
            ))}
          </select>
          <input
            type="text"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Notes (optional)"
            className="w-full text-sm border rounded px-2 py-1"
          />
          <div className="flex gap-2">
            <button
              onClick={handleCreate}
              disabled={!formatName.trim() || !fileId}
              className="px-3 py-1 text-sm text-white bg-blue-600 rounded disabled:opacity-50"
            >
              Save
            </button>
            <button
              onClick={() => setShowForm(false)}
              className="px-3 py-1 text-sm text-gray-600 hover:bg-gray-100 rounded"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
```

### Step 5 — Commit

- [ ] **Step 5a: Commit**

```bash
git add frontend/src/
git commit -m "feat(frontend): works schemas/hooks, WorkFilesTable with master crown, FormatOverridePanel"
```

---

## Task 16: AssociatedWorks Page (Optimistic Master Toggle)

**Files:**
- Create: `src/pages/library/AssociatedWorks.tsx`
- Modify: `src/main.tsx`

### Step 1 — AssociatedWorks

- [ ] **Step 1a: Create `src/pages/library/AssociatedWorks.tsx`**

```tsx
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, RotateCcw } from "lucide-react";

import { useWorkDetail, useSetMaster, useRevertMaster } from "@/api/works";
import { useArtistDetail } from "@/api/artists";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";
import { WorkFilesTable } from "@/components/domain/works/WorkFilesTable";
import { FormatOverridePanel } from "@/components/domain/works/FormatOverridePanel";

export default function AssociatedWorks() {
  const { artist_id, work_id } = useParams<{
    artist_id: string;
    work_id: string;
  }>();
  const navigate = useNavigate();
  const { data: artist } = useArtistDetail(artist_id);
  const { data: work, isLoading } = useWorkDetail(work_id);
  const setMaster = useSetMaster(work_id!);
  const revertMaster = useRevertMaster(work_id!);

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  if (!work) {
    return <div className="text-gray-500">Work not found</div>;
  }

  return (
    <>
      <div className="mb-4">
        <button
          onClick={() => navigate(`/library/artists/${artist_id}`)}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" /> Back to {artist?.name ?? "artist"}
        </button>
      </div>

      <PageHeader
        title={work.title}
        description={artist?.name}
        actions={
          work.song_master && (
            <button
              onClick={() => revertMaster.mutate()}
              disabled={revertMaster.isPending}
              className="flex items-center gap-2 px-3 py-2 text-sm border rounded-md hover:bg-gray-50"
            >
              <RotateCcw className="h-4 w-4" /> Revert to Auto
            </button>
          )
        }
      />

      {/* Master info */}
      {work.song_master && (
        <div className="mb-4 text-sm text-gray-500 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2">
          Master: <span className="font-medium">{work.song_master.selection_method}</span>
          {work.song_master.score !== null && ` (score: ${work.song_master.score})`}
        </div>
      )}

      {/* Files table */}
      <div className="mb-6">
        <WorkFilesTable
          recordings={work.recordings}
          masterFileId={work.song_master?.preferred_file_id ?? null}
          masterMethod={work.song_master?.selection_method ?? null}
          onSetMaster={(fileId) => setMaster.mutate(fileId)}
        />
      </div>

      {/* Format overrides */}
      <FormatOverridePanel
        workId={work.id}
        overrides={work.format_overrides}
        recordings={work.recordings}
      />
    </>
  );
}
```

### Step 2 — Wire + commit

- [ ] **Step 2a: Update router, commit**

```typescript
import AssociatedWorks from "@/pages/library/AssociatedWorks";
// Replace works placeholder
```

```bash
git add frontend/src/
git commit -m "feat(frontend): AssociatedWorks — work detail with master toggle and format overrides"
```

---

## Task 17: Settings + PathConfiguration Pages

**Files:**
- Modify: `src/lib/schemas/settings.ts`
- Create: `src/api/settings.ts`
- Create: `src/pages/settings/Settings.tsx`
- Create: `src/pages/settings/PathConfiguration.tsx`
- Modify: `src/main.tsx`, `src/lib/schemas/index.ts`

### Step 1 — Settings schemas + hooks

- [ ] **Step 1a: Replace `src/lib/schemas/settings.ts`**

```typescript
import { z } from "zod";

export const SettingsMapSchema = z.record(z.string(), z.string());
export type SettingsMap = z.infer<typeof SettingsMapSchema>;

export const SettingEntrySchema = z.object({
  key: z.string(),
  value: z.string(),
});
export type SettingEntry = z.infer<typeof SettingEntrySchema>;

// Stub compat
export const SettingsSchema = SettingsMapSchema;
export const PathConfigSchema = z.object({
  local_path_prefix: z.string().optional(),
  navidrome_path_prefix: z.string().optional(),
});
```

- [ ] **Step 1b: Update index.ts**

Add: `export * from "./settings";`

- [ ] **Step 1c: Create `src/api/settings.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type { SettingsMap, SettingEntry } from "@/lib/schemas/settings";

export function useSettings() {
  return useQuery<SettingsMap>({
    queryKey: ["settings"],
    queryFn: () => apiFetch<SettingsMap>("/api/v1/settings"),
  });
}

export function useUpdateSetting() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) =>
      apiFetch<SettingEntry>(`/api/v1/settings/${encodeURIComponent(key)}`, {
        method: "PUT",
        body: JSON.stringify({ value }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}
```

### Step 2 — Settings page

- [ ] **Step 2a: Create `src/pages/settings/Settings.tsx`**

```tsx
import { Link } from "react-router-dom";
import { FolderCog } from "lucide-react";

import { useSettings } from "@/api/settings";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";

export default function Settings() {
  const { data: settings, isLoading } = useSettings();

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  const entries = Object.entries(settings ?? {});

  return (
    <>
      <PageHeader title="Settings" />

      <div className="max-w-xl space-y-6">
        {/* Quick links */}
        <Link
          to="/settings/paths"
          className="flex items-center gap-3 p-4 bg-white border rounded-lg hover:shadow-sm transition-shadow"
        >
          <div className="p-2 bg-blue-50 rounded-lg">
            <FolderCog className="h-5 w-5 text-blue-600" />
          </div>
          <div>
            <div className="font-semibold">Path Configuration</div>
            <div className="text-sm text-gray-500">
              Configure library paths and Navidrome path mapping
            </div>
          </div>
        </Link>

        {/* All settings */}
        <div className="bg-white border rounded-lg p-5">
          <h3 className="font-semibold mb-3">All Settings</h3>
          {entries.length === 0 ? (
            <p className="text-sm text-gray-400">No settings configured yet.</p>
          ) : (
            <div className="divide-y">
              {entries.map(([key, value]) => (
                <div key={key} className="py-2 flex justify-between text-sm">
                  <span className="font-mono text-gray-600">{key}</span>
                  <span className="text-gray-900 truncate max-w-xs" title={value}>
                    {value}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
```

### Step 3 — PathConfiguration page

- [ ] **Step 3a: Create `src/pages/settings/PathConfiguration.tsx`**

```tsx
import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Save } from "lucide-react";

import { useSettings, useUpdateSetting } from "@/api/settings";
import { PageHeader } from "@/components/ui/PageHeader";
import { Spinner } from "@/components/ui/Spinner";

const PATH_KEYS = [
  {
    key: "local_path_prefix",
    label: "Local Library Path",
    placeholder: "e.g. D:\\Music",
    description: "The local filesystem prefix where your music files live.",
  },
  {
    key: "navidrome_path_prefix",
    label: "Navidrome Path Prefix",
    placeholder: "e.g. /data/music",
    description: "The path prefix Navidrome uses to access the same files.",
  },
];

export default function PathConfiguration() {
  const navigate = useNavigate();
  const { data: settings, isLoading } = useSettings();
  const updateSetting = useUpdateSetting();
  const [values, setValues] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  // Initialize from settings
  useEffect(() => {
    if (settings) {
      const initial: Record<string, string> = {};
      for (const { key } of PATH_KEYS) {
        initial[key] = settings[key] ?? "";
      }
      setValues(initial);
    }
  }, [settings]);

  async function handleSave() {
    setSaved(false);
    for (const { key } of PATH_KEYS) {
      const value = values[key]?.trim() ?? "";
      if (value !== (settings?.[key] ?? "")) {
        await updateSetting.mutateAsync({ key, value });
      }
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-8 w-8 text-gray-400" />
      </div>
    );
  }

  return (
    <>
      <div className="mb-4">
        <button
          onClick={() => navigate("/settings")}
          className="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700"
        >
          <ArrowLeft className="h-4 w-4" /> Back to settings
        </button>
      </div>

      <PageHeader
        title="Path Configuration"
        description="Configure how M3U exports map local paths to Navidrome paths."
        actions={
          <button
            onClick={handleSave}
            disabled={updateSetting.isPending}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-md disabled:opacity-50"
          >
            <Save className="h-4 w-4" />
            {updateSetting.isPending ? "Saving..." : "Save"}
          </button>
        }
      />

      {saved && (
        <div className="mb-4 text-sm text-green-600 bg-green-50 border border-green-200 rounded-md px-4 py-2">
          Settings saved successfully.
        </div>
      )}

      <div className="max-w-xl space-y-4">
        {PATH_KEYS.map(({ key, label, placeholder, description }) => (
          <div key={key} className="bg-white border rounded-lg p-5">
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {label}
            </label>
            <p className="text-xs text-gray-500 mb-2">{description}</p>
            <input
              type="text"
              value={values[key] ?? ""}
              onChange={(e) =>
                setValues((prev) => ({ ...prev, [key]: e.target.value }))
              }
              placeholder={placeholder}
              className="w-full rounded-md border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        ))}
      </div>
    </>
  );
}
```

### Step 4 — Wire + commit

- [ ] **Step 4a: Update router, commit**

```typescript
import Settings from "@/pages/settings/Settings";
import PathConfiguration from "@/pages/settings/PathConfiguration";
// Replace both settings placeholders
```

```bash
git add frontend/src/
git commit -m "feat(frontend): Settings + PathConfiguration — settings management with Navidrome path mapping"
```

---

## Final Gate

- [ ] **Remove all remaining Placeholder references from `src/main.tsx`**

The `Placeholder` component and all its usages should be gone — every route should point to a real page component.

- [ ] **Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: No errors.

- [ ] **Full visual verification**

Start the backend (`honcho start` or `uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000`) and frontend (`cd frontend && npm run dev`). Walk through every page:

1. `/stations` — list, create, delete stations
2. `/stations/:id` — edit, CSV upload
3. `/stations/:id/playlists` — playlist list, event table, calendar, M3U export
4. `/matcher` — queue, resolve artists/identities, file search
5. `/matcher/scanner` — library scan
6. `/library` — status overview
7. `/library/artists` — virtual scroll, search
8. `/library/artists/:id` — works list
9. `/library/artists/:id/works/:id` — recordings, files, master toggle, format overrides
10. `/settings` — all settings
11. `/settings/paths` — path configuration
12. Progress bar — trigger a scan, verify progress updates via WebSocket

**Phase 4 gate passed when:** Each page renders with real data from the running backend.

---

## Note: CORS Configuration

If the frontend dev server (port 5173) gets CORS errors talking to the backend (port 8000), add CORS middleware to `backend/main.py`:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

This should be added as the first step if CORS issues are encountered during development.
