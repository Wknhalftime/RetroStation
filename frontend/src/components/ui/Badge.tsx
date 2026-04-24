import { cn } from "@/lib/utils";

type BadgeVariant = "default" | "success" | "warning" | "danger" | "info";

const variantClasses: Record<BadgeVariant, string> = {
  default: "bg-gray-100 text-gray-700",
  success: "bg-green-100 text-green-800",
  warning: "bg-yellow-100 text-yellow-800",
  danger: "bg-red-100 text-red-800",
  info: "bg-blue-100 text-blue-800",
};

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  className?: string;
}

export function Badge({ children, variant = "default", className }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        variantClasses[variant],
        className
      )}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// MatchStatusBadge helper
// ---------------------------------------------------------------------------

type MatchStatus = "matched" | "pending" | "conflict" | "unmatched" | string;

const matchStatusVariant: Record<string, BadgeVariant> = {
  matched: "success",
  pending: "warning",
  conflict: "danger",
  unmatched: "default",
};

interface MatchStatusBadgeProps {
  status: MatchStatus;
  className?: string;
}

export function MatchStatusBadge({ status, className }: MatchStatusBadgeProps) {
  const variant = matchStatusVariant[status] ?? "info";
  return (
    <Badge variant={variant} className={className}>
      {status}
    </Badge>
  );
}
