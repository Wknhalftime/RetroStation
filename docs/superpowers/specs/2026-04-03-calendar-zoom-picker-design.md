# Calendar Zoom Picker — Design Spec

**Date:** 2026-04-03
**Status:** Approved

## Problem

The station calendar currently navigates month-to-month using chevron buttons. Stations can span decades of broadcast data, making it tedious to reach distant dates. There is no way to jump to a specific year, and the calendar is scoped to individual playlists rather than the station as a whole.

## Solution

Replace the month-only calendar with an in-place zoom picker that drills down through three levels: **Year → Month → Day**. Scope the calendar to the station level (union of all playlists) and make date selection filter events across all playlists for that station on the chosen date.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Navigation model | In-place zoom (year → month → day) | Familiar mobile pattern, minimal footprint, no dropdowns |
| Data scope | Station-level broadcast days | Users think in terms of "what aired on station X on date Y", not playlist IDs |
| Year grid contents | Only years with data | Eliminates dead clicks; compact grid regardless of date range span |
| Month grid | All 12 months shown; months without data dimmed/disabled | Gives temporal context while preventing dead clicks |
| Day grid | Existing calendar behavior preserved | Blue = has broadcasts, indigo = selected, gray = no data |
| Playlist list | Removed from PlaylistViewer, moved to StationDashboard | Calendar becomes primary navigation; playlist list becomes import inventory |
| Date selection behavior | Filters events across all playlists for station+date | Answers "what aired that day?" regardless of which CSV it came from |

## Calendar Zoom Levels

### Level 1: Year Grid
- 3-column grid of years
- Only years with broadcast data for the station are shown
- Clicking a year transitions to the month grid for that year
- Header: "Select Year" (not clickable — this is the top level)

### Level 2: Month Grid
- 3×4 grid of month abbreviations (Jan–Dec)
- Months with broadcast data: blue background, clickable
- Months without data: gray text, disabled
- Header: the year (e.g., "2001"), clickable — returns to year grid
- Clicking a month transitions to the day grid for that month and calls `onMonthChange(new Date(year, monthIndex, 1))` to sync the parent

### Level 3: Day Grid
- Existing 7-column calendar layout, largely unchanged
- Header: "Month Year" (e.g., "January 2001"), clickable — returns to month grid
- Chevron arrows for adjacent month navigation remain
- When chevron navigation crosses a year boundary (e.g., Dec 2001 → Jan 2002), the internal `selectedYear` updates to match the new month's year
- Blue circle: has broadcasts. Indigo circle: selected. Gray: no data.
- Clicking a broadcast day triggers `onSelect(isoDate)`

### Zoom-Out Affordance
- The header text at levels 2 and 3 is styled as a clickable link (indigo color, dotted underline) to signal interactivity
- Clicking it moves up one level

### Initial View
- Calendar opens at the year grid level so the user always starts with the big picture

## API Changes

### New Endpoint: Station Broadcast Days

```
GET /api/v1/stations/{station_id}/broadcast-days
Response: string[]  (ISO date strings, sorted ascending)
```

SQL:
```sql
SELECT DISTINCT broadcast_date
FROM broadcast_days
WHERE station_id = :station_id
ORDER BY broadcast_date
```

Returns all unique broadcast dates for the station, unioned across all playlists. Verifies station exists first (404 if not found). Requires auth token (`_token: Token`).

### New Endpoint: Station Events by Date

```
GET /api/v1/stations/{station_id}/events?date=YYYY-MM-DD&limit=50&offset=0
Response: {
  items: Array<{
    id: string,
    played_at: string,
    artist_name: string,
    title: string,
    match_status: string,
    match_tier: string,
    playlist_name: string   // NEW: identifies source playlist
  }>,
  total: number
}
```

SQL joins `log_events` → `log_identities` → `log_artists`, plus `log_events` → `playlists` for the playlist name. Filtered by `le.playlist_id IN (SELECT id FROM playlists WHERE station_id = :station_id)` and `le.played_at::date = :date`. Paginated with limit/offset. Verifies station exists first (404 if not found). Requires auth token (`_token: Token`).

### New Endpoint: Station M3U Export by Date

```
POST /api/v1/stations/{station_id}/export-m3u
Body: { "date": "YYYY-MM-DD" }
Response: text/x-mpegurl (M3U file download)
```

Uses POST to match the existing playlist export pattern (`@router.post("/{playlist_id}/export-m3u")`) and the `apiDownload` client helper which is hardwired to `method: "POST"`.

The existing `generate_m3u()` service is playlist-scoped — it calls `event_repo.get_by_playlist(playlist_id)`. To support station+date export:

1. Add a new abstract method to `LogEventRepository`: `get_by_station_date(station_id: UUID, date: date) -> list[LogEvent]`
2. Implement in `PgLogEventRepository`: query `log_events` joined to `playlists` filtered by `station_id` and `played_at::date = :date`
3. Generalize `generate_m3u()` to accept an `events: list[LogEvent]` parameter instead of fetching internally. The caller fetches events (either by playlist or by station+date) and passes them in.
4. The new endpoint fetches station events via the repository, then passes them to the generalized `generate_m3u()`.

Downloaded filename: `{call_letters}-{date}.m3u` (e.g., `WABC-2001-01-05.m3u`). Requires auth token and station existence check.

## Backend Schema Changes

### New Pydantic model: `StationEventItem`

Located in `backend/routers/stations.py`:

```python
class StationEventItem(BaseModel):
    id: UUID
    played_at: datetime
    artist_name: str
    title: str
    match_status: str
    match_tier: str | None
    playlist_name: str

class StationPaginatedEvents(BaseModel):
    items: list[StationEventItem]
    total: int
```

### New abstract method on `LogEventRepository`

Located in `backend/repositories/log_events.py`:

```python
@abstractmethod
def get_by_station_date(self, station_id: UUID, date: date) -> list[LogEvent]: ...
```

Concrete implementation in `backend/db/repositories/log_events.py` (`PgLogEventRepository`).

### `generate_m3u()` signature change

Current: `generate_m3u(*, playlist_id: UUID, event_repo: ..., ...)`
New: `generate_m3u(*, events: list[LogEvent], identity_repo: ..., ...)`

The `playlist_id` parameter is removed. Callers fetch events themselves and pass the list in. Both the existing playlist export endpoint and the new station+date export endpoint call the same `generate_m3u()` function — they just fetch events differently.

### Existing caller update: `_generate_m3u_sync` in `playlists.py`

The existing `_generate_m3u_sync()` wrapper (lines 299–328 of `backend/routers/playlists.py`) currently calls `generate_m3u(playlist_id=pid, event_repo=repos.log_events, ...)`. After the signature change, this wrapper must be updated to fetch events first via `repos.log_events.get_by_playlist(pid)` and then pass the resulting list to `generate_m3u(events=events, ...)`. This preserves backward compatibility of the existing playlist export endpoint.

### Performance note

The station events endpoint filters with `played_at::date = :date`. For large stations, consider adding an index on `played_at` to `log_events` if query performance is slow. No migration required upfront — monitor and add if needed.

## Frontend Changes

### New Zod schemas: `StationEventItem` and `StationPaginatedEvents`

Located in `frontend/src/lib/schemas/stations.ts` (extend existing file):

```typescript
export const StationEventItemSchema = z.object({
  id: z.string().uuid(),
  played_at: z.string(),
  artist_name: z.string(),
  title: z.string(),
  match_status: z.string(),
  match_tier: z.string().nullable(),
  playlist_name: z.string(),
});
export type StationEventItem = z.infer<typeof StationEventItemSchema>;

export const StationPaginatedEventsSchema = z.object({
  items: z.array(StationEventItemSchema),
  total: z.number(),
});
export type StationPaginatedEvents = z.infer<typeof StationPaginatedEventsSchema>;
```

### DatePicker.tsx — Refactored

**Props (unchanged shape):**
- `broadcastDays: string[]` — now station-level instead of playlist-level
- `selectedDate: string | undefined`
- `onSelect: (date: string) => void`
- `month: Date` — current calendar month (used at day level)
- `onMonthChange: (month: Date) => void`

**New internal state:**
- `view: 'years' | 'months' | 'days'` — tracks current zoom level
- `selectedYear: number | undefined` — the year being viewed at month level

**Derived data (computed from broadcastDays via `useMemo`):**
- `availableYears: number[]` — unique years extracted from broadcast dates
- `availableMonths(year): Set<number>` — months with data for a given year
- `broadcastSet: Set<string>` — existing, for day-level lookups

**Zoom interactions:**
- Clicking a month in the month grid: sets `view = 'days'`, calls `onMonthChange(new Date(year, monthIndex, 1))` to sync parent state
- Chevron navigation at day level: calls `onMonthChange` as before. When the new month crosses a year boundary, also updates internal `selectedYear` to `newMonth.getFullYear()`
- Clicking the day-level header ("January 2001"): sets `view = 'months'`, `selectedYear = month.getFullYear()`
- Clicking the month-level header ("2001"): sets `view = 'years'`, clears `selectedYear`

### PlaylistViewer.tsx — Simplified

**Removed:**
- Playlist list sidebar and all playlist selection state (`selectedId`, playlist dropdown)
- Per-playlist broadcast days fetch (`useBroadcastDays` from `@/api/playlists`)
- Per-playlist events fetch (`usePlaylistEvents` from `@/api/playlists`)
- Import of `PlaylistSummary` type
- Import of `usePlaylists` hook
- Import of `useExportM3u` mutation (replaced by `useExportStationM3u` from `@/api/stations`)

**Changed:**
- Fetches station-level broadcast days: `useStationBroadcastDays(station_id)` from `@/api/stations`
- Sidebar contains only the calendar
- Main content: when no date selected, empty state reads "Select a date from the calendar to view broadcasts"
- When date selected: renders `StationEventTable` (renamed component) with `stationId` + `date` props
- PageHeader description updated to "Broadcast calendar"
- Export M3U button: disabled when `selectedDate` is undefined. Label: "Export M3U for {selectedDate}". Uses `useExportStationM3u` hook.

### PlaylistEventTable.tsx → StationEventTable.tsx (renamed and relocated)

Moves from `components/domain/playlists/` to `components/domain/stations/` (alongside existing `StationForm.tsx`). Import path in `PlaylistViewer.tsx` updates to `@/components/domain/stations/StationEventTable`.

**New props interface:**
```typescript
interface StationEventTableProps {
  stationId: string;
  date: string;
}
```

**Internal changes:**
- Fetches data via `useStationEvents(stationId, date, PAGE_SIZE, offset)` instead of `usePlaylistEvents`
- Adds "Playlist" column between "Time" and "Artist" columns, displaying `event.playlist_name`
- Empty state copy updated to: "No broadcast events recorded for this date."
- Error state copy updated to: "Failed to load broadcast events."
- Offset resets to 0 when `date` changes (via `useEffect`)

### StationDashboard.tsx — Uploaded Playlists Section

- New section below the CSV upload card
- Uses existing `usePlaylists(station_id)` hook from `@/api/playlists`
- Renders a simple list: playlist name + event count
- Read-only inventory — no navigation actions

### api/stations.ts — New Query Hooks

```typescript
const stationBroadcastDaysKey = (stationId: string) =>
  ["stations", stationId, "broadcast-days"] as const;

const stationEventsKey = (stationId: string, date: string, limit: number, offset: number) =>
  ["stations", stationId, "events", { date, limit, offset }] as const;

export function useStationBroadcastDays(stationId: string | undefined) {
  return useQuery<string[]>({
    queryKey: stationBroadcastDaysKey(stationId ?? ""),
    queryFn: () => apiFetch<string[]>(`/api/v1/stations/${stationId}/broadcast-days`),
    enabled: Boolean(stationId),
  });
}

export function useStationEvents(
  stationId: string | undefined,
  date: string | undefined,
  limit: number,
  offset: number,
) {
  return useQuery<StationPaginatedEvents>({
    queryKey: stationEventsKey(stationId ?? "", date ?? "", limit, offset),
    queryFn: () =>
      apiFetch<StationPaginatedEvents>(
        `/api/v1/stations/${stationId}/events?date=${date}&limit=${limit}&offset=${offset}`,
      ),
    enabled: Boolean(stationId) && Boolean(date),
  });
}
```

### New export mutation: `useExportStationM3u`

Located in `api/stations.ts`. Requires adding `apiDownload` to the imports from `@/api/client` and `StationPaginatedEvents` from `@/lib/schemas/stations`.

```typescript
interface ExportStationM3uVariables {
  stationId: string;
  date: string;
  callLetters: string;
}

export function useExportStationM3u() {
  return useMutation<void, Error, ExportStationM3uVariables>({
    mutationFn: async ({ stationId, date, callLetters }) => {
      const blob = await apiDownload(
        `/api/v1/stations/${stationId}/export-m3u`,
        { date },
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${callLetters}-${date}.m3u`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
  });
}
```

## What Does NOT Change

- Backend `broadcast_days` table schema — no migrations needed
- Existing playlist-level endpoints — kept for backward compatibility (may be cleaned up later)
- Tailwind styling approach — consistent with existing components
- No new frontend dependencies
- Existing playlist-level M3U export endpoint (still works, just no longer called from PlaylistViewer)

## Testing Strategy

- **Backend:** Unit tests for the three new station endpoints (broadcast-days returns correct union, events-by-date returns cross-playlist results with playlist_name, export-m3u generates valid M3U for station+date)
- **Backend:** Test that `generate_m3u()` works with the new `events` parameter signature (pass events from both playlist and station+date callers)
- **Frontend:** Manual verification of zoom level transitions, disabled state for empty months/days, date selection triggering correct API call
- **Frontend:** Verify chevron navigation across year boundaries updates correctly, zoom-out header links work at each level
- **Edge cases:** Station with no playlists (empty year grid), station with one day of data (single year → single month → single day), year with sparse months, chevron from Dec to Jan across year boundary
