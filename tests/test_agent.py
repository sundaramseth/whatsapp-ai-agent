import asyncio
import hashlib
import hmac
import json
import time

import httpx
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app, process_one
from app.store import Store


def config(tmp_path):
    return Settings(_env_file=None, WORKER_ENABLED=False, DATABASE_PATH=str(tmp_path/'db.sqlite'), WHATSAPP_VERIFY_TOKEN='test-verify',
                    META_APP_SECRET='test-secret', WHATSAPP_ACCESS_TOKEN='test-access', WHATSAPP_PHONE_NUMBER_ID='123',
                    META_GRAPH_VERSION='v-test', ADMIN_API_KEY='test-admin')


def message(identifier='m1', text='price'):
    return {'id': identifier, 'from': '919999999999', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': text}}


def event(msg):
    return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'value': {'metadata': {'phone_number_id': '123'},
            'contacts': [{'wa_id': msg['from'], 'profile': {'name': 'Example'}}], 'messages': [msg]}}]}]}


def signed(payload):
    raw = json.dumps(payload).encode()
    return raw, {'x-hub-signature-256': 'sha256='+hmac.new(b'test-secret', raw, hashlib.sha256).hexdigest()}


def test_verification_and_configuration(tmp_path):
    with TestClient(create_app(Settings(_env_file=None, DATABASE_PATH=str(tmp_path/'empty.db')))) as client:
        assert client.get('/ready').status_code == 503
        assert client.get('/webhook', params={'hub.mode':'subscribe','hub.verify_token':'x','hub.challenge':'42'}).status_code == 403
    with TestClient(create_app(config(tmp_path))) as client:
        assert client.get('/ready').status_code == 200
        assert client.get('/webhook',params={'hub.mode':'subscribe','hub.verify_token':'test-verify','hub.challenge':'42'}).text == '42'
        assert client.get('/webhook',params={'hub.mode':'subscribe','hub.verify_token':'test-verify'}).status_code == 400


def test_signed_webhook_deduplicates_and_admin_is_private(tmp_path):
    app = create_app(config(tmp_path))
    # Disable delivery while testing HTTP ingestion; no real HTTP calls.
    with TestClient(app) as client:
        raw, headers = signed(event(message()))
        assert client.post('/webhook',content=raw).status_code == 401
        assert client.post('/webhook',content=raw,headers=headers).status_code == 200
        assert client.post('/webhook',content=raw,headers=headers).status_code == 200
        assert app.state.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0] == 1
        assert client.get('/admin/leads').status_code == 401
        result=client.get('/admin/leads',headers={'Authorization':'Bearer test-admin'})
        assert result.headers['cache-control']=='no-store'
        assert len(result.json()['leads'])==1


def test_routing_handoff_optout_and_resume(tmp_path):
    store=Store(config(tmp_path).DATABASE_PATH); sent=[]
    async def sender(phone, text): sent.append(text); return 'out'+str(len(sent))
    def run(identifier,text):
        store.ingest(message(identifier,text),'Example')
        asyncio.run(process_one(store,config(tmp_path),sender))
    run('m1','price'); assert '₹7,999' in sent[-1]
    run('m2','HUMAN'); assert store.lead('919999999999')['paused']==1
    run('m3','hello'); assert len(sent)==2
    run('m4','STOP'); assert store.lead('919999999999')['opted_out']==1
    run('m5','hello'); assert len(sent)==3
    run('m6','START'); assert store.lead('919999999999')['opted_out']==0
    run('m7','hello'); assert len(sent)==5
    store.db.close()


def test_expired_messages_never_send(tmp_path):
    store=Store(config(tmp_path).DATABASE_PATH)
    msg=message();msg['timestamp']=str(int(time.time()-86401));store.ingest(msg,'Example')
    async def sender(*args): raise AssertionError('Must not send expired messages')
    asyncio.run(process_one(store,config(tmp_path),sender))
    assert store.db.execute('SELECT state FROM inbox').fetchone()[0]=='expired'
    store.db.close()


def test_timeout_is_not_blindly_retried(tmp_path):
    store=Store(config(tmp_path).DATABASE_PATH);store.ingest(message(),'Example')
    async def sender(*args): raise httpx.ReadTimeout('timeout')
    asyncio.run(process_one(store,config(tmp_path),sender))
    assert store.db.execute('SELECT state FROM inbox').fetchone()[0]=='uncertain'
    assert store.next_message() is None
    store.db.close()


def test_restart_reconciles_inflight_and_purge(tmp_path):
    cfg=config(tmp_path);store=Store(cfg.DATABASE_PATH);store.ingest(message(),'Example')
    store.update_message('m1',state='sending');store.db.close()
    store=Store(cfg.DATABASE_PATH)
    assert store.db.execute('SELECT state FROM inbox').fetchone()[0]=='uncertain'
    old=message('old');old['timestamp']=str(int(time.time()-40*86400));store.ingest(old,'Example')
    store.purge(30)
    assert store.db.execute("SELECT COUNT(*) FROM inbox WHERE id='old'").fetchone()[0]==0
    store.db.close()


def test_manual_reply_is_authenticated_idempotent_and_window_limited(tmp_path):
    app=create_app(config(tmp_path));headers={'Authorization':'Bearer test-admin'}
    with TestClient(app) as client:
        app.state.store.ingest(message(),'Example')
        path='/admin/leads/919999999999/reply'
        body={'id':'manual-12345678','text':'Thanks! I will review your requirements.'}
        assert client.post(path,json=body).status_code==401
        assert client.post(path,json=body,headers=headers).json()['status']=='queued'
        assert client.post(path,json=body,headers=headers).json()['status']=='already queued'
        assert client.post(path,json={**body,'text':'different'},headers=headers).status_code==409
        assert app.state.store.lead('919999999999')['paused']==1
        sent=[]
        async def sender(phone,text): sent.append(text);return 'out-manual'
        # The original inbound is paused, but the explicitly queued human reply is delivered.
        asyncio.run(process_one(app.state.store,config(tmp_path),sender))
        asyncio.run(process_one(app.state.store,config(tmp_path),sender))
        assert sent==[body['text']]
        app.state.store.update_lead('919999999999',opted_out=1)
        assert client.post(path,json={**body,'id':'manual-23456789'},headers=headers).status_code==409
        assert client.get('/inbox').status_code==200


def test_wrong_business_number_and_malformed_payload_do_not_create_leads(tmp_path):
    app=create_app(config(tmp_path))
    with TestClient(app) as client:
        payload=event(message());payload['entry'][0]['changes'][0]['value']['metadata']['phone_number_id']='999'
        raw,headers=signed(payload)
        assert client.post('/webhook',content=raw,headers=headers).status_code==200
        assert app.state.store.leads()==[]
        raw,headers=signed({'object':'wrong'})
        assert client.post('/webhook',content=raw,headers=headers).status_code==400
