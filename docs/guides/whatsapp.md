# Send a WhatsApp run summary

Set `notificationProvider` to `"twilio"` and `notificationRecipient` to the
opted-in recipient's E.164 number in your user `~/.atelier/settings.json`.
Save the following fields in `~/.atelier/twilio.json` and restrict this file
to your OS user. The sender reads it locally when you invoke `atelier notify`.

```json
{
  "account_sid": "AC...",
  "auth_token": "...",
  "sender": "+12025550111",
  "content_sid": "HX..."
}
```

Use a registered WhatsApp sender and an approved text template with three
variables in order: run ID, short status and space-separated PR links (or
`None`). After a run, use `atelier notify RUN_ID --link URL`. A trusted final
shell step can use `atelier notify "$ATELIER_FLOW_ID" --status "drafts ready"
--link "$OUTPUT_PUBLISH"`. Repeat `--link` for multiple PRs. Only canonical
`https://github.com/OWNER/REPO/pull/NUMBER` links go into the summary. Local
artifacts remain in run outputs.

`atelier logs RUN_ID` shows each attempt. An accepted message has a Twilio
message SID. The sender retries a rate limit once. If it persists, invoke
`atelier notify RUN_ID` later to reuse the saved summary. A timeout, ambiguous
provider response or interrupted submission leaves delivery uncertain.
Check Twilio's message history before explicitly using `--retry-uncertain`.
