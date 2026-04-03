import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
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
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
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
  const year = month.getFullYear();
  const monthIndex = month.getMonth();

  const broadcastSet = new Set(broadcastDays);
  const days = getDaysInMonth(year, monthIndex);
  const leadingBlanks = startDayOfWeek(year, monthIndex);

  const goToPrev = () => {
    const d = new Date(year, monthIndex - 1, 1);
    onMonthChange(d);
  };

  const goToNext = () => {
    const d = new Date(year, monthIndex + 1, 1);
    onMonthChange(d);
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
        <span className="text-sm font-semibold text-gray-800">
          {MONTH_NAMES[monthIndex]} {year}
        </span>
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
        {/* Leading blank cells */}
        {Array.from({ length: leadingBlanks }).map((_, i) => (
          <span key={`blank-${i}`} />
        ))}

        {/* Day cells */}
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
                !isSelected && hasBroadcast &&
                  "bg-blue-100 font-medium text-blue-700 hover:bg-blue-200 cursor-pointer",
                !hasBroadcast &&
                  "text-gray-300 cursor-not-allowed",
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
