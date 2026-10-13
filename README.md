# Sundaram WhatsApp enquiry agent
FastAPI + official Meta Cloud API. Supports signed webhook verification, persistent inbound deduplication, queued replies, guided pricing/service qualification, optional Gemini generation, protected lead export, STOP/START, human handoff and delivery-status capture.

## Local setup
Use Python 3.12+. Create a virtual environment, `pip install -r requirements-dev.txt`, copy `.env.example` to `.env` and fill server settings. Never commit credentials. Run `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`. Tests: `python -m pytest -q`.

`/health` is process health. `/ready` returns 503 until required credentials exist; its ready response is a configuration check, not proof Meta accepted credentials or delivered messages. Missing Gemini settings use guided replies. Enable `AI_ENABLED=true` only with your chosen supported `GEMINI_MODEL` and `GEMINI_API_KEY` and after reviewing the customer privacy notice. Chat history (last four exchanges) and new text then go to Google Gemini. No model response can execute tools or confirm a booking.

## Production deployment
Use an always-on single-instance service with a persistent disk. `render.yaml` is a paid-service blueprint (review costs before applying), or use Docker with a `/data` persistent volume. **Do not deploy this SQLite worker on Vercel serverless, an ephemeral disk, multiple replicas or multiple Uvicorn workers.** Keep backups of the database. For scale, migrate to PostgreSQL plus a shared job queue before adding replicas.

Configure these as hosting secrets: `WHATSAPP_VERIFY_TOKEN` (random token), `META_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN` (appropriately scoped long-lived token), `WHATSAPP_PHONE_NUMBER_ID`, `META_GRAPH_VERSION` (currently supported version selected in Meta), `ADMIN_API_KEY` (different strong random token). Use dashboard secret fields, not chat or Git. Test with Meta's test number before connecting the live business number.

Meta setup:
1. Create/select your authorized Meta Business app with WhatsApp product. Verify the phone number and appropriate business permissions.
2. Webhook callback: `https://YOUR-BACKEND/webhook`. Verify token: exactly the server `WHATSAPP_VERIFY_TOKEN`. Subscribe to `messages` and subscribe the app to your WABA.
3. Confirm phone-number ID matches the number receiving website enquiries (`+91 9140910217`, as configured in the website). The ID is not the phone number.
4. If retaining the existing WhatsApp Business phone app, use Meta's supported **Business App coexistence onboarding** where your account/provider is eligible. Do not delete/migrate your existing account just to attach this webhook. Onboarding and eligibility must be confirmed in your own Meta account.
5. Send a genuine test text from an authorized test recipient; verify accepted, delivered and read statuses. Then test HUMAN, STOP and START. Only after these pass should the live number be enabled.

Official references:
- https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started
- https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview
- https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users
- https://ai.google.dev/api/generate-content

## Human operations & lead export
Protected endpoints require `Authorization: Bearer ADMIN_API_KEY`; use a private API client, never append the key to URLs or expose it in website JS.
- GET `/admin/leads`: contact, stage, pause, opt-out and last inbound timestamp.
- GET `/admin/leads.csv`: up to 1,000 leads for deliberate mapping/import into the existing business sheet. **No automatic Google Sheets sync is configured.** CSV has its own schema; map fields into Leads rather than overwriting existing columns.
- POST `/admin/leads/{phone}/reply` with `{"id":"manual-unique-uuid","text":"Your reply"}` queues a human response and pauses the assistant.
- GET `/admin/issues`: failed, expired and uncertain sends needing investigation.
- PATCH `/admin/leads/{phone}` with `{"paused":true}` to take over or false to resume. This does not remove customer opt-out.
- DELETE `/admin/leads/{phone}` removes that lead and stored inbox history after a verified deletion request.

HUMAN pauses the assistant and flags Needs human; it does not send owner notifications. Open `/inbox` over HTTPS and enter the admin access key to review recent messages, pause/resume the assistant and queue your own reply. The key stays in page memory and clears on logout. Manual replies use a client-generated idempotency ID and the same 23-hour safety window; queued does not mean delivered. Check the delivery issues list and Meta/provider inbox. With eligible coexistence, the Business app is another human reply option. STOP suppresses further automated messages; START restores them. No unsolicited campaigns or automatic follow-ups are sent.

## Reliability & limits
The webhook queues before acknowledging and deduplicates by Meta message ID. Replies older than 23 hours are suppressed to leave a safety margin inside the service window. HTTP 429 retries up to three times; other rejections need operator intervention. A network timeout or worker crash during send is **uncertain**, not blindly retried, because Meta may already have accepted the message. Reconcile delivery manually. Database persistence cannot guarantee exactly-once network delivery. Incoming message and send status timestamps may arrive out of order. Model replies are best-effort and should be reviewed in launch testing.

Content is retained for 30 days by default; purge runs hourly. Opt-out suppression retains only phone/state after expiry. Logs exclude message bodies and tokens. Protect disk/backups, rotate tokens, and apply the published privacy notice. Customer phone names and message text are personal data; keep exports private.
