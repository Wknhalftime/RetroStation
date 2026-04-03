import { Inbox } from "lucide-react";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  title?: string;
  description?: string;
  actions?: React.ReactNode;
  className?: string;
}

export function EmptyState({
  title = "Nothing here yet",
  description,
  actions,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-gray-300 p-12 text-center",
        className,
      )}
    >
      <Inbox className="mx-auto mb-4 h-12 w-12 text-gray-300" aria-hidden="true" />
      <p className="text-base font-medium text-gray-500">{title}</p>
      {description && (
        <p className="mt-1 text-sm text-gray-400">{description}</p>
      )}
      {actions && <div className="mt-4">{actions}</div>}
    </div>
  );
}
