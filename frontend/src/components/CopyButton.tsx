import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";

/**
 * Copies `getText()` on click and flips its label to "copied" for a moment.
 * `navigator.clipboard` only exists in secure contexts (https or localhost),
 * so a page served over http on a LAN address gets a toast instead of a silent
 * no-op; the text next to the button stays selectable for a manual copy.
 */
export function CopyButton({
  label,
  getText,
  testId,
}: {
  label: string;
  getText: () => string;
  testId: string;
}) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  const copy = () => {
    if (!navigator.clipboard) {
      toast.error("Clipboard unavailable here. Select the text and copy it by hand.");
      return;
    }
    navigator.clipboard
      .writeText(getText())
      .then(() => setCopied(true))
      .catch(() => toast.error("Couldn't copy to the clipboard"));
  };

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="font-mono text-micro"
      onClick={copy}
      data-testid={testId}
    >
      {copied ? "copied" : label}
    </Button>
  );
}
