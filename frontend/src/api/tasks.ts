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
