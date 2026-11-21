"""Send one run summary, retaining the decision to retry in the run log."""
import re
import time
from datetime import UTC, datetime

from pydantic import ValidationError

from flow_atelier.core.settings import AtelierSettings
from flow_atelier.schemas.log import LogEntry
from flow_atelier.services.notification.base import (
    NotificationProvider,
    RateLimited,
    Rejected,
    Uncertain,
)
from flow_atelier.services.notification.twilio import PHONE, Twilio
from flow_atelier.services.store.events import RunJournal, file_lock

PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*")


def send_summary(
    journal: RunJournal, provider: NotificationProvider, recipient: str,
    status: str | None, links: list[str], *, retry_uncertain: bool = False,
) -> str:
    """Serialize sends including manual retries and recovery after a process exit."""
    run = journal.path.stem
    journal.side.mkdir(exist_ok=True)
    with file_lock(journal.side / ".notify.lock", wait=False) as acquired:
        if not acquired:
            raise ValueError("another notification attempt is active")
        previous = {}
        for kind, row in journal.rows():
            if kind == "log" and row["values"].get("tool") == "notification:twilio":
                previous = row["values"]["extra"]

        delivery = previous.get("delivery")
        if delivery == "accepted":
            return "accepted " + previous["receipt"]
        if delivery == "uncertain" and not retry_uncertain:
            raise ValueError("delivery uncertain; check Twilio before using --retry-uncertain")

        urls = list(dict.fromkeys(url for url in links if PR_URL.fullmatch(url)))
        if previous:
            if (recipient != previous["recipient"] or (status and status != previous["status"])
                    or (links and urls != previous["links"])):
                raise ValueError("retry must use the original recipient and summary")
            status, urls = previous["status"], previous["links"]
        status = status or journal.latest("progress")["status"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ,_-]{0,79}", status):
            raise ValueError("use a plain-text status of up to 80 characters")

        def record(state: str, detail: str = "", receipt: str = "") -> str:
            now = datetime.now(UTC).isoformat()
            message = f"{state} {detail or receipt}".strip()
            entry = LogEntry(
                task="notification", tool="notification:twilio", output=message,
                started_at=now, finished_at=now, exit_code=int(state != "accepted"),
                extra={"delivery": state, "recipient": recipient, "status": status,
                       "links": urls, "receipt": receipt},
            )
            journal.append("log", entry.model_dump(mode="json"))
            return message

        retried = False
        while True:
            # If the process exits during POST, a second invocation must stop here.
            record("uncertain", "submitting to Twilio")
            try:
                receipt = provider.send(recipient, run, status, urls)
            except RateLimited:
                message = record("retryable", "Twilio rate limit")
                if retried:
                    raise ValueError(message) from None
                retried = True
                time.sleep(1)
            except Rejected as exc:
                raise ValueError(record("rejected", str(exc))) from None
            except Uncertain:
                raise ValueError(record("uncertain", "check Twilio before retrying")) from None
            else:
                return record("accepted", receipt=receipt)


def notify_configured(
    settings: AtelierSettings, journal: RunJournal, status: str | None, links: list[str],
    *, retry_uncertain: bool = False,
) -> str:
    """Load sender credentials in the trusted notification step."""
    recipient = settings.notification_recipient or ""
    if settings.notification_provider != "twilio" or not re.fullmatch(PHONE, recipient):
        raise ValueError("configure notificationProvider (twilio) and an E.164 recipient")
    path = settings.global_atelier_dir / "twilio.json"
    try:
        sender = Twilio.model_validate_json(path.read_bytes())
    except ValidationError:
        raise ValueError(f"invalid Twilio settings in {path}") from None
    return send_summary(journal, sender, recipient, status, links, retry_uncertain=retry_uncertain)
