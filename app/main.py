import asyncio
import contextlib
import csv
import hashlib
import hmac
import io
import json
import logging
import re
import time
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.agent import generate_reply
from app.config import settings
from app.store import Store

logger = logging.getLogger('whatsapp_agent')


def configured(config):
    return all([config.WHATSAPP_VERIFY_TOKEN, config.META_APP_SECRET, config.WHATSAPP_ACCESS_TOKEN,
                config.WHATSAPP_PHONE_NUMBER_ID, config.META_GRAPH_VERSION, config.ADMIN_API_KEY])


async def process_one(store, config, sender=None):
    message = store.next_message()
    if not message:
        return False
    phone, identifier, text = message['phone'], message['id'], message['body']
    lead = store.lead(phone)
    # Respond only to a recent customer-initiated message, never schedule unsolicited follow-ups.
    if time.time() - message['timestamp'] >= 23 * 3600 or message['timestamp'] > time.time() + 300:
        store.update_message(identifier, state='expired', error='Outside safe reply window')
        return True
    command = text.strip().casefold()
    if message['kind'] == 'manual' and lead['opted_out']:
        store.update_message(identifier, state='skipped', error='Customer opted out before send')
        return True
    if message['kind'] == 'manual':
        reply = text
    elif message['reply']:
        reply = message['reply']
    elif command in {'stop', 'unsubscribe', 'बंद'}:
        store.update_lead(phone, opted_out=1, paused=1, stage='Opted out')
        reply = 'Automatic replies stopped. Type START to resume. For deletion contact sundaramseth755@gmail.com.'
    elif command in {'start', 'resume'}:
        store.update_lead(phone, opted_out=0, paused=0, stage='New')
        reply = 'Automatic replies resumed. What would you like to build? Type HUMAN to speak with Sundaram.'
    elif lead['opted_out'] or lead['paused']:
        store.update_message(identifier, state='skipped')
        return True
    elif command in {'human', 'agent', 'person', 'sundaram', 'इंसान'}:
        store.update_lead(phone, paused=1, stage='Needs human')
        reply = 'Sundaram ke personal follow-up ke liye enquiry flag kar di hai. Automatic replies pause hain. Reply ka exact time confirm nahi hai. Urgent: sundaramseth755@gmail.com. START se assistant resume karein.'
    elif text == '[Unsupported message]':
        reply = 'Please send your requirement as text, or type HUMAN for Sundaram. This assistant cannot read attachments yet.'
    else:
        reply = message['reply'] or await generate_reply(text, store.history(phone), config)
        store.update_lead(phone, stage='Contacted')
    store.update_message(identifier, state='sending', reply=reply, attempts=message['attempts'] + 1)
    try:
        if sender:
            outbound_id = await sender(phone, reply)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f'https://graph.facebook.com/{config.META_GRAPH_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages',
                    headers={'Authorization': f'Bearer {config.WHATSAPP_ACCESS_TOKEN}'},
                    json={'messaging_product': 'whatsapp', 'to': phone, 'type': 'text',
                          'text': {'body': reply, 'preview_url': False}})
                response.raise_for_status()
                outbound_id = response.json()['messages'][0]['id']
        store.update_message(identifier, state='sent', outbound_id=outbound_id, error=None)
    except httpx.HTTPStatusError as exc:
        # A rejected request can retry; never log tokens, message text or response body.
        retry = exc.response.status_code == 429 and message['attempts'] < 3
        store.update_message(identifier, state='pending' if retry else 'failed', available=time.time()+60,
                             error=f'Meta HTTP {exc.response.status_code}')
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        store.update_message(identifier, state='uncertain', error='Send outcome uncertain; reconcile before retry')
    return True


async def worker(store, config):
    last_purge = 0
    while True:
        try:
            if time.time() - last_purge > 3600:
                store.purge(config.RETENTION_DAYS)
                last_purge = time.time()
            if not config.WORKER_ENABLED or not configured(config) or not await process_one(store, config):
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error('Worker error; inspect service and protected inbox')
            await asyncio.sleep(2)


def create_app(config=settings):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(config.DATABASE_PATH)
        app.state.store = store
        task = asyncio.create_task(worker(store, config))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        store.db.close()

    app = FastAPI(title='Sundaram WhatsApp Enquiry Agent', version='2.0.0', lifespan=lifespan)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'service': 'whatsapp-ai-agent'}

    @app.get('/ready')
    async def ready():
        if not configured(config):
            raise HTTPException(503, 'Required WhatsApp/admin configuration missing')
        return {'status': 'ready', 'mode': 'ai' if config.AI_ENABLED and config.GEMINI_API_KEY and config.GEMINI_MODEL else 'guided'}

    @app.get('/webhook')
    async def verify(hub_mode: str = Query('', alias='hub.mode'),
                     hub_verify_token: str = Query('', alias='hub.verify_token'),
                     hub_challenge: str = Query('', alias='hub.challenge')):
        if not hub_challenge or not hub_mode or not hub_verify_token:
            raise HTTPException(400, 'Missing verification parameters')
        if not config.WHATSAPP_VERIFY_TOKEN or hub_mode != 'subscribe' or not hmac.compare_digest(hub_verify_token, config.WHATSAPP_VERIFY_TOKEN):
            raise HTTPException(403, 'Verification failed')
        return Response(hub_challenge, media_type='text/plain')

    @app.post('/webhook')
    async def receive(request: Request):
        if not configured(config):
            raise HTTPException(503, 'WhatsApp configuration missing')
        # Bound request streaming before parsing to avoid unrestricted payload allocation.
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > 1_000_000:
                raise HTTPException(413, 'Payload too large')
            chunks.append(chunk)
        raw = b''.join(chunks)
        expected = 'sha256=' + hmac.new(config.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get('x-hub-signature-256', ''), expected):
            raise HTTPException(401, 'Invalid signature')
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get('object') != 'whatsapp_business_account':
                raise ValueError()
            for entry in payload.get('entry', []):
                for change in entry.get('changes', []):
                    value = change.get('value', {})
                    if str(value.get('metadata', {}).get('phone_number_id', '')) != config.WHATSAPP_PHONE_NUMBER_ID:
                        continue
                    contacts = {c['wa_id']: c.get('profile', {}).get('name', '') for c in value.get('contacts', [])}
                    for message in value.get('messages', []):
                        if not re.fullmatch(r'\d{7,15}', message.get('from', '')) or not message.get('id'):
                            raise ValueError()
                        app.state.store.ingest(message, contacts.get(message['from'], ''))
                    for status in value.get('statuses', []):
                        app.state.store.status(status['id'], status['status'])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(400, 'Invalid webhook payload')
        return {'status': 'queued'}

    def admin(request):
        token = request.headers.get('authorization', '')
        if not config.ADMIN_API_KEY or not hmac.compare_digest(token, 'Bearer ' + config.ADMIN_API_KEY):
            raise HTTPException(401, 'Unauthorized')

    @app.get('/inbox', include_in_schema=False)
    async def inbox_page():
        return Response(Path(__file__).with_name('inbox.html').read_text(), media_type='text/html', headers={'Cache-Control':'no-store','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer'})

    @app.get('/admin/leads')
    async def leads(request: Request, limit: int = Query(100, ge=1, le=1000)):
        admin(request)
        return {'leads': app.state.store.leads(limit)}

    @app.get('/admin/issues')
    async def issues(request: Request):
        admin(request)
        return {'issues': app.state.store.issues()}

    @app.get('/admin/leads.csv')
    async def export(request: Request):
        admin(request)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Lead ID', 'Business/contact', 'Phone/WhatsApp', 'Stage', 'Last inbound UTC', 'Human handoff', 'Opted out'])
        for lead in app.state.store.leads(1000):
            name = lead['name'] or ''
            if name.startswith(('=', '+', '-', '@', '\t', '\r')):
                name = "'" + name
            writer.writerow(['WA-'+lead['phone'], name, lead['phone'], lead['stage'], lead['last_inbound'], lead['paused'], lead['opted_out']])
        return Response(output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="whatsapp-leads.csv"', 'Cache-Control': 'no-store'})

    class LeadUpdate(BaseModel):
        paused: bool

    @app.patch('/admin/leads/{phone}')
    async def update(phone: str, body: LeadUpdate, request: Request):
        admin(request)
        if not app.state.store.lead(phone):
            raise HTTPException(404, 'Lead not found')
        # Opt-out remains effective even when a human resumes the assistant.
        app.state.store.update_lead(phone, paused=int(body.paused))
        return {'status': 'updated'}

    class ManualReply(BaseModel):
        id: str = Field(pattern=r'^manual-[a-zA-Z0-9-]{8,80}$')
        text: str = Field(min_length=1, max_length=3500)

    @app.post('/admin/leads/{phone}/reply')
    async def manual_reply(phone: str, body: ManualReply, request: Request):
        admin(request)
        lead = app.state.store.lead(phone)
        if not lead:
            raise HTTPException(404, 'Lead not found')
        if lead['opted_out'] or time.time()-lead['last_inbound'] >= 23*3600:
            raise HTTPException(409, 'Opted out or outside safe reply window')
        if not body.text.strip():
            raise HTTPException(400, 'Message cannot be blank')
        try:
            created = app.state.store.queue_manual(body.id, phone, body.text.strip())
        except ValueError:
            raise HTTPException(409, 'Idempotency key already used')
        return {'status':'queued' if created else 'already queued'}

    @app.delete('/admin/leads/{phone}')
    async def delete(phone: str, request: Request):
        admin(request)
        app.state.store.delete_lead(phone)
        return {'status': 'deleted'}

    @app.middleware('http')
    async def private_admin(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/admin'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    return app


app = create_app()
