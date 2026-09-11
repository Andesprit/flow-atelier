import { useState } from "react";
import { Bell, BellOff } from "lucide-react";
import { toast } from "sonner";
import { notificationsOn } from "@/hooks/useRunNotifications";
import { NOTIFY_STORAGE_KEY } from "@/constants/dashboard";
import { cn } from "@lib/cn";

/**
 * Opt in to desktop notifications for runs. The browser's permission prompt
 * only ever appears from this click, never from a run or a page load. Absent
 * where the Notification API is missing (iOS Safari outside a home-screen
 * app, a page served over plain http on a LAN address).
 */
export function NotifyToggle({ className }: { className?: string }) {
  const [on, setOn] = useState(notificationsOn);
  if (typeof Notification === "undefined") return null;

  const toggle = async () => {
    if (on) {
      // Only the local choice; the browser permission is the person's to keep.
      localStorage.removeItem(NOTIFY_STORAGE_KEY);
      setOn(false);
      return;
    }
    const permission =
      Notification.permission === "default"
        ? await Notification.requestPermission()
        : Notification.permission;
    if (permission === "granted") {
      localStorage.setItem(NOTIFY_STORAGE_KEY, "1");
      setOn(true);
      toast.success("You'll be notified when a run finishes or waits on you");
    } else if (permission === "denied") {
      toast.error("Notifications are blocked for this site in your browser settings");
    }
    // A dismissed prompt leaves the permission undecided: stay off, say nothing.
  };

  const label = on
    ? "Turn off run notifications"
    : "Notify me when a run finishes or waits on me";

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={on}
      aria-label={label}
      title={label}
      data-testid="notify-toggle"
      className={cn(
        "inline-flex size-11 shrink-0 items-center justify-center rounded-sm border border-border text-muted-foreground hover:text-foreground hover:bg-muted focus-visible:outline-2 focus-visible:outline-primary",
        on && "text-primary",
        className,
      )}
    >
      {on ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
    </button>
  );
}
