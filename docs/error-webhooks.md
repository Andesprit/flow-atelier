# Receive signed application errors

Start `atelier serve-hooks` on the machine that owns the target repository. It listens on
`127.0.0.1:8001`. Configure each source in the user's `~/.atelier/settings.json` and
restrict that file to the user. Restart the receiver after editing it.

```json
{
  "webhookSources": {
    "orders": {"project": "/absolute/path/to/orders", "conduit": "error-to-pr"}
  },
  "webhookSecrets": {"orders": "<per-source random secret>"},
  "runCaps": {"errors": 2}
}
```

Install the selected conduit before enabling its source. It receives one input,
`report_b64`, containing the exact signed JSON bytes in standard Base64. Decode it
as data inside the trusted intake step. All report text can originate from an
application user.

## Signing a request

Send UTF-8 JSON to `POST /hooks/errors/orders`. Set `X-Atelier-Timestamp` to the
current Unix time in whole seconds. Set `X-Atelier-Signature` to `sha256=`
followed by the lowercase hex HMAC-SHA256 of the ASCII timestamp, a literal
period and the exact request bytes, keyed with the UTF-8 source secret.

```python
timestamp = str(int(time.time()))
signature = "sha256=" + hmac.digest(
    secret.encode(), timestamp.encode() + b"." + body, "sha256"
).hex()
```

Reports may include an application-defined `fingerprint`. Otherwise send an
`exception_class` and an ordered, nonempty list of stack `frames` with stable
module and function names. Line numbers and message text may change between
occurrences. For example:

```json
{"exception_class":"KeyError","frames":[{"module":"orders.checkout","function":"save","line":24}]}
```

A valid request returns HTTP 202 with `run_id` and `status`. The same source,
project and fingerprint always return the original ID. After history cleanup,
`status` is `history_expired`. The receiver accepts up to 64 KiB and five minutes of
clock skew in either direction. Up to two error runs execute per machine by default.
When all 100 waiting positions are occupied, new errors get HTTP 429 with
`Retry-After`. Previously claimed errors can still be looked up by resending them.
Restarting drains waiting work. A run interrupted after its journal was published
keeps its ID and can be inspected locally before resuming.

## HTTPS relay

Use a dedicated TLS virtual host in Apache with `mod_proxy`, `mod_proxy_http`
and `mod_alias`. Configure its certificate and key through the site's usual TLS
settings and include the supplied [webhook relay configuration](webhook-relay.conf).
It forwards the error path to the receiver's loopback port and returns 404 on
other paths, including `/flows`. If you set `serve-hooks --port`, update the
relay target to match.
