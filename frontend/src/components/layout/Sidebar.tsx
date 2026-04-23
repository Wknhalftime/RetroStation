import { NavLink } from "react-router-dom";
import { Radio, Library, GitCompare, Settings } from "lucide-react";
import { cn } from "@/lib/utils";
import { useProgressStore } from "@/store/progressStore";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/stations", label: "Stations", icon: Radio },
  { to: "/library", label: "Library", icon: Library },
  { to: "/matcher", label: "Resolution Center", icon: GitCompare },
  { to: "/settings", label: "Settings", icon: Settings },
];

const TASK_TYPE_TO_NAV: Record<string, string> = {
  scan: "/library",
  library_enrichment: "/library",
  mb_enrichment: "/library",
  ingestion: "/stations",
  matching: "/matcher",
  m3u_export: "/stations",
  rules_apply: "/stations",
  // Settings intentionally absent — no tasks route there.
};

export function Sidebar() {
  const runningTasks = useProgressStore((s) => s.runningTasks);

  return (
    <aside className="fixed left-0 top-0 h-full w-64 bg-gray-900 flex flex-col z-40">
      {/* Logo */}
      <div className="px-6 py-5 border-b border-gray-800">
        <span className="text-white text-lg font-bold tracking-wide">
          RetroStation
        </span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => {
          const showDot = runningTasks.some(
            (t) => TASK_TYPE_TO_NAV[t.task_type] === to,
          );

          return (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-gray-800 text-white"
                    : "text-gray-400 hover:bg-gray-800 hover:text-white"
                )
              }
            >
              <Icon className="h-5 w-5 flex-shrink-0" />
              {label}
              {showDot && (
                <span className="ml-auto h-2 w-2 rounded-full bg-blue-500 animate-pulse" />
              )}
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}
