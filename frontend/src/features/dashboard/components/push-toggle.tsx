import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, BASE_URL, fetchJson } from "@/services/client";

async function fetchPushKey(): Promise<string | null> {
  try {
    const { key } = await fetchJson<{ key: string }>(`${BASE_URL}/push/key`, undefined, { method: "GET" });
    return key;
  } catch (e) {
    if (e instanceof ApiError && e.status === 503) return null;
    throw e;
  }
}

export function PushToggle() {
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const [device, setDevice] = useState<PushSubscription | null>(null);
  const [pushKey, setPushKey] = useState<string | null>();
  const [busy, setBusy] = useState(supported);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!supported) return;
    let active = true;
    const subscription = navigator.serviceWorker.getRegistration().then((reg) => reg?.pushManager.getSubscription())
      .then((current) => { if (active) setDevice(current ?? null); })
      .catch(() => { if (active) setMessage("Could not read this device’s notification settings."); });
    const key = fetchPushKey()
      .then((current) => { if (active) setPushKey(current); })
      .catch(() => { /* Retry the key request if the user enables notifications. */ });
    void Promise.all([subscription, key]).then(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [supported]);

  useEffect(() => {
    if (!device) return;
    let active = true;
    const refresh = () => {
      if (document.hidden) return;
      fetchJson<{ lastError: string | null }>(BASE_URL + "/push/status", { endpoint: device.endpoint })
        .then(({ lastError }) => { if (active) setMessage(lastError ?? ""); })
        .catch((e) => { if (active) setMessage(e.message); });
    };
    refresh();
    const timer = window.setInterval(refresh, 30000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      active = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [device]);

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
      const key = pushKey ?? await fetchPushKey();
      setPushKey(key);
      if (key === null) return;
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

  if (supported && !device && pushKey === null) return null;

  return (
    <section className="grid gap-2 border-t border-border pt-4">
      {supported ? (
        <Button variant="outline" className="whitespace-normal" disabled={busy} onClick={toggle}>
          {device ? "Disable completion notifications" : "Enable completion notifications"}
        </Button>
      ) : <p>Completion notifications require a supported secure browser or Home Screen app.</p>}
      {supported && <p>Receive completions from every project on this machine, including CLI and scheduled runs.</p>}
      {message && <p role="status" className="text-muted-foreground">{message}</p>}
    </section>
  );
}

