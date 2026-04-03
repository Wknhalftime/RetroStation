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
