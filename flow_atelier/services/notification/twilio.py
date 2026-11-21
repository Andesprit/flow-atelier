"""Submit an approved WhatsApp template to Twilio over HTTPS."""
import base64
import json
import re
from http.client import HTTPException, HTTPSConnection
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from flow_atelier.services.notification.base import RateLimited, Rejected, Uncertain

PHONE = r"^\+[1-9][0-9]{1,14}$"


class Twilio(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    account_sid: str = Field(pattern=r"^AC[0-9a-fA-F]{32}$")
    auth_token: SecretStr = Field(min_length=1)
    sender: str = Field(pattern=PHONE)
    content_sid: str = Field(pattern=r"^HX[0-9a-fA-F]{32}$")

    def send(self, recipient: str, run: str, status: str, links: list[str]) -> str:
        form = urlencode({
            "From": "whatsapp:" + self.sender,
            "To": "whatsapp:" + recipient,
            "ContentSid": self.content_sid,
            "ContentVariables": json.dumps({"1": run, "2": status, "3": " ".join(links) or "None"}),
        })
        credentials = f"{self.account_sid}:{self.auth_token.get_secret_value()}".encode()
        conn = HTTPSConnection("api.twilio.com", timeout=10)
        try:
            conn.request("POST", f"/2010-04-01/Accounts/{self.account_sid}/Messages.json", form, {
                "Authorization": "Basic " + base64.b64encode(credentials).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
            })
            response = conn.getresponse()
            if response.status == 429:
                raise RateLimited
            if 400 <= response.status < 500 and response.status != 408:
                raise Rejected(f"Twilio rejected the request (HTTP {response.status})")
            if response.status != 201:
                raise Uncertain
            receipt = json.loads(response.read())["sid"]
            if not isinstance(receipt, str) or not re.fullmatch(r"SM[0-9a-fA-F]{32}", receipt):
                raise Uncertain
            return receipt
        except (HTTPException, OSError, ValueError, KeyError, TypeError):
            raise Uncertain from None
        finally:
            conn.close()

