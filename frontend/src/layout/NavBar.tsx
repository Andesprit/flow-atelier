import { NavLink } from "react-router-dom";
import { cn } from "@lib/cn";
import { ThemeToggle } from "./ThemeToggle";
import { NotifyToggle } from "./NotifyToggle";

const NAV = [
  { to: "/dashboard", label: "dashboard" },
  { to: "/designer", label: "designer" },
  { to: "/kanban", label: "kanban" },
] as const;

export function TopBar() {
  return (
    <header
      data-testid="top-bar"
      className="sticky top-0 z-sticky flex h-14 items-center gap-2 border-b border-border bg-background/90 px-3 backdrop-blur sm:gap-6 sm:px-6"
    >
      {/* mr-auto replaces the empty aria-hidden flex-1 spacer that used to sit
          between the wordmark and the nav. min-w-0 + truncate lets the wordmark
          give way rather than pushing the theme toggle off-screen: at 390px the
          three 44px nav targets plus the toggle leave it very little room. */}
      <NavLink
        to="/dashboard"
        aria-label="flow-atelier home"
        className="mr-auto flex h-11 shrink-0 items-center whitespace-nowrap"
      >
        {/* Drops the "flow-" half below sm rather than ellipsizing the brand:
            three 44px nav targets plus the theme toggle leave under 180px here,
            and a wordmark reading "flow-ate…" looks broken rather than tight. */}
        <span className="font-display text-lg leading-none sm:text-xl">
          <span className="hidden sm:inline">flow-</span>
          <em className="text-primary italic">atelier</em>
        </span>
      </NavLink>

      <nav className="flex shrink-0 items-center" aria-label="primary">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) =>
              cn(
                "flex h-11 items-center rounded-sm px-1.5 font-mono text-mini uppercase tracking-[0.08em] text-muted-foreground hover:text-foreground sm:px-2.5 sm:text-label sm:tracking-[0.12em]",
                isActive && "text-foreground bg-muted",
              )
            }
          >
            {n.label}
          </NavLink>
        ))}
      </nav>
      <NotifyToggle />
      <ThemeToggle />
    </header>
  );
}
