import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { StationList } from "@/pages/stations/StationList";
import { StationDashboard } from "@/pages/stations/StationDashboard";
import { PlaylistViewer } from "@/pages/stations/PlaylistViewer";
import { MatcherBrowser } from "@/pages/matcher/MatcherBrowser";
import { ScannerActions } from "@/pages/matcher/ScannerActions";
import { LibraryStatus } from "@/pages/library/LibraryStatus";
import { ArtistBrowser } from "@/pages/library/ArtistBrowser";
import { ArtistDetail } from "@/pages/library/ArtistDetail";
import { AssociatedWorks } from "@/pages/library/AssociatedWorks";
import { Settings } from "@/pages/settings/Settings";
import { PathConfiguration } from "@/pages/settings/PathConfiguration";
import "@/index.css";

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/stations" replace /> },
      { path: "stations", element: <StationList /> },
      {
        path: "stations/:station_id",
        element: <StationDashboard />,
      },
      {
        path: "stations/:station_id/playlists",
        element: <PlaylistViewer />,
      },
      { path: "library", element: <LibraryStatus /> },
      { path: "library/artists", element: <ArtistBrowser /> },
      { path: "library/artists/:artist_id", element: <ArtistDetail /> },
      {
        path: "library/artists/:artist_id/works/:work_id",
        element: <AssociatedWorks />,
      },
      { path: "matcher", element: <MatcherBrowser /> },
      { path: "matcher/scanner", element: <ScannerActions /> },
      { path: "settings", element: <Settings /> },
      { path: "settings/paths", element: <PathConfiguration /> },
    ],
  },
]);

// ---------------------------------------------------------------------------
// Query client
// ---------------------------------------------------------------------------

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 2,
    },
  },
});

// ---------------------------------------------------------------------------
// Mount
// ---------------------------------------------------------------------------

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>
);
