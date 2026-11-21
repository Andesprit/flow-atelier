import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { BASE_URL, fetchJson } from "@/services/client";

export function PushToggle() {
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const [device, setDevice] = useState<PushSubscription | null>(null);
  const [busy, setBusy] = useState(supported);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!supported) return;
    navigator.serviceWorker.getRegistration().then((reg) => reg?.pushManager.getSubscription())
      .then((subscription) => setDevice(subscription ?? null))
      .catch(() => setMessage("Could not read this device’s notification settings."))
      .finally(() => setBusy(false));
  }, [supported]);

  async function toggle() {
    setBusy(true);
    setMessage("");
    try {
      if (device) {
        await fetchJson(`${BASE_URL}/push/subscriptions`, { endpoint: device.endpoint }, { method: "DELETE" });
        await device.unsubscribe();
        setDevice(null);
        return;
      }
      if (await Notification.requestPermission() !== "granted") {
        setMessage("Notifications are off. If blocked in your browser settings, enable them there.");
        return;
      }
      const { key } = await fetchJson<{ key: string }>(`${BASE_URL}/push/key`, undefined, { method: "GET" });
      await navigator.serviceWorker.register("/sw.js");
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: key,
      });
      try {
        const { endpoint, keys } = subscription.toJSON();
        await fetchJson(`${BASE_URL}/push/subscriptions`, { endpoint, keys });
        setDevice(subscription);
      } catch (e) {
        await subscription.unsubscribe();
        throw e;
      }
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Could not change completion notifications.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="grid gap-2 border-t border-border pt-4">
      {supported ? (
        <Button variant="outline" className="whitespace-normal" disabled={busy} onClick={toggle}>
          {device ? "Disable completion notifications" : "Enable completion notifications"}
        </Button>
      ) : <p>Completion notifications require a supported secure browser or Home Screen app.</p>}
      {message && <p role="status" className="text-muted-foreground">{message}</p>}
    </section>
  );
}

