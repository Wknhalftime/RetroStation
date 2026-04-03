import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type View = "years" | "months" | "days";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function getDaysInMonth(year: number, month: number): Date[] {
  const days: Date[] = [];
  const date = new Date(year, month, 1);
  while (date.getMonth() === month) {
    days.push(new Date(date));
    date.setDate(date.getDate() + 1);
  }
  return days;
}

function startDayOfWeek(year: number, month: number): number {
  return new Date(year, month, 1).getDay(); // 0 = Sunday
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const MONTH_ABBRS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

const DAY_LABELS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DatePickerProps {
  broadcastDays: string[];
  selectedDate: string | undefined;
  onSelect: (date: string) => void;
  month: Date;
  onMonthChange: (month: Date) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DatePicker({
  broadcastDays,
  selectedDate,
  onSelect,
  month,
  onMonthChange,
}: DatePickerProps) {
  const [view, setView] = useState<View>("years");
  const [selectedYear, setSelectedYear] = useState<number | undefined>(
    undefined,
  );

  // Derived data from broadcastDays
  const { availableYears, monthsByYear, broadcastSet } = useMemo(() => {
    const set = new Set(broadcastDays);
    const yearSet = new Set<number>();
    const mByY = new Map<number, Set<number>>();

    for (const iso of broadcastDays) {
      const y = parseInt(iso.slice(0, 4), 10);
      const m = parseInt(iso.slice(5, 7), 10) - 1; // 0-indexed
      yearSet.add(y);
      if (!mByY.has(y)) mByY.set(y, new Set());
      mByY.get(y)!.add(m);
    }

    return {
      availableYears: Array.from(yearSet).sort((a, b) => a - b),
      monthsByYear: mByY,
      broadcastSet: set,
    };
  }, [broadcastDays]);

  // --- Year Grid ---
  if (view === "years") {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="mb-3 text-center">
          <span className="text-sm font-semibold text-gray-800">
            Select Year
          </span>
        </div>
        {availableYears.length === 0 ? (
          <p className="text-center text-xs text-gray-400">
            No broadcast data available
          </p>
        ) : (
          <div className="grid grid-cols-3 gap-2">
            {availableYears.map((y) => (
              <button
                key={y}
                type="button"
                onClick={() => {
                  setSelectedYear(y);
                  setView("months");
                }}
                className="rounded-lg bg-indigo-50 px-2 py-2 text-sm font-semibold text-indigo-700 transition hover:bg-indigo-100"
              >
                {y}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  // --- Month Grid ---
  if (view === "months" && selectedYear !== undefined) {
    const activeMonths = monthsByYear.get(selectedYear) ?? new Set<number>();
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
        <div className="mb-3 text-center">
          <button
            type="button"
            onClick={() => {
              setView("years");
              setSelectedYear(undefined);
            }}
            className="text-sm font-semibold text-indigo-600 underline decoration-dotted underline-offset-2 hover:text-indigo-800"
          >
            {selectedYear}
          </button>
        </div>
        <div className="grid grid-cols-3 gap-2">
          {MONTH_ABBRS.map((abbr, i) => {
            const hasData = activeMonths.has(i);
            return (
              <button
                key={abbr}
                type="button"
                disabled={!hasData}
                onClick={() => {
                  onMonthChange(new Date(selectedYear, i, 1));
                  setView("days");
                }}
                className={cn(
                  "rounded-lg px-2 py-2 text-sm transition",
                  hasData &&
                    "bg-blue-50 font-medium text-blue-700 hover:bg-blue-100 cursor-pointer",
                  !hasData && "text-gray-300 cursor-not-allowed",
                )}
              >
                {abbr}
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // --- Day Grid ---
  const year = month.getFullYear();
  const monthIndex = month.getMonth();
  const days = getDaysInMonth(year, monthIndex);
  const leadingBlanks = startDayOfWeek(year, monthIndex);

  const goToPrev = () => {
    const d = new Date(year, monthIndex - 1, 1);
    onMonthChange(d);
    setSelectedYear(d.getFullYear());
  };

  const goToNext = () => {
    const d = new Date(year, monthIndex + 1, 1);
    onMonthChange(d);
    setSelectedYear(d.getFullYear());
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm">
      {/* Month navigation */}
      <div className="mb-3 flex items-center justify-between">
        <button
          type="button"
          onClick={goToPrev}
          className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
          aria-label="Previous month"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => {
            setView("months");
            setSelectedYear(year);
          }}
          className="text-sm font-semibold text-indigo-600 underline decoration-dotted underline-offset-2 hover:text-indigo-800"
        >
          {MONTH_NAMES[monthIndex]} {year}
        </button>
        <button
          type="button"
          onClick={goToNext}
          className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700"
          aria-label="Next month"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>

      {/* Day-of-week headers */}
      <div className="mb-1 grid grid-cols-7 text-center">
        {DAY_LABELS.map((label) => (
          <span key={label} className="text-xs font-medium text-gray-400">
            {label}
          </span>
        ))}
      </div>

      {/* Calendar grid */}
      <div className="grid grid-cols-7 gap-y-0.5">
        {Array.from({ length: leadingBlanks }).map((_, i) => (
          <span key={`blank-${i}`} />
        ))}

        {days.map((day) => {
          const iso = isoDate(day);
          const hasBroadcast = broadcastSet.has(iso);
          const isSelected = iso === selectedDate;

          return (
            <button
              key={iso}
              type="button"
              onClick={() => hasBroadcast && onSelect(iso)}
              disabled={!hasBroadcast}
              aria-label={`${iso}${hasBroadcast ? " — has broadcasts" : ""}`}
              aria-pressed={isSelected}
              className={cn(
                "mx-auto flex h-7 w-7 items-center justify-center rounded-full text-xs transition",
                isSelected &&
                  "bg-indigo-600 font-semibold text-white",
                !isSelected &&
                  hasBroadcast &&
                  "bg-blue-100 font-medium text-blue-700 hover:bg-blue-200 cursor-pointer",
                !hasBroadcast && "text-gray-300 cursor-not-allowed",
              )}
            >
              {day.getDate()}
            </button>
          );
        })}
      </div>
    </div>
  );
}
