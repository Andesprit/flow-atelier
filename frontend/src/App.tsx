import { useEffect, lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ThemeProvider } from "@/context/ThemeContext";
import { AppFrame } from "@/layout/AppFrame";
import { ConduitProvider } from "@/services/ConduitProvider";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Toaster } from "@/components/ui/toaster";
import Dashboard from "@/pages/dashboard";
import * as runner from "@/runner";

const Designer = lazy(() => import("@/pages/Designer"));
const Kanban = lazy(() => import("@/pages/kanban"));
const Run = lazy(() => import("@/pages/Run"));

// Expose the runner on window for smoke checks — dev only, so the debug hook
// is not part of the shipped surface.
if (import.meta.env.DEV && typeof window !== "undefined") {
  (window as unknown as { atelier?: typeof runner }).atelier = runner;
}

// A skeleton in the shape of a route rather than a spinner in the middle of the
// screen: the Designer chunk is the heaviest one, and a page-shaped placeholder
// reads as "this is loading" instead of "something is wrong".
function ScreenFallback() {
  return (
    <div aria-busy="true" aria-live="polite" className="px-4 py-6 lg:px-10 lg:py-10">
      <span className="sr-only">Loading</span>
      <div className="mb-8 h-10 w-64 animate-pulse rounded bg-muted lg:h-14" />
      <div className="mx-auto grid max-w-[1280px] gap-4 lg:grid-cols-3">
        {[0, 1, 2].map((col) => (
          <div key={col} className="space-y-2">
            <div className="h-4 w-24 animate-pulse rounded bg-muted" />
            {[0, 1].map((row) => (
              <div
                key={row}
                className="h-20 animate-pulse rounded-sm border border-border bg-muted/40"
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  useEffect(() => {
    runner.bootRunner();
  }, []);
  return (
    <ThemeProvider>
      <ConduitProvider>
        <BrowserRouter>
          <AppFrame>
            <ErrorBoundary>
              <Suspense fallback={<ScreenFallback />}>
                <Routes>
                  <Route path="/" element={<Navigate to="/dashboard" replace />} />
                  <Route path="/dashboard" element={<Dashboard />} />
                  <Route path="/designer" element={<Designer />} />
                  <Route path="/kanban" element={<Kanban />} />
                  <Route path="/runs/:flowId" element={<Run />} />
                </Routes>
              </Suspense>
            </ErrorBoundary>
          </AppFrame>
          <Toaster />
        </BrowserRouter>
      </ConduitProvider>
    </ThemeProvider>
  );
}
