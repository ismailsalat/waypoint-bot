"""Integration tests for the merged dashboard and the actual AutoScheduler path.

No real credentials or Discord requests are used.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from app.accounts.models import Account, ServerTarget
from app.accounts.store import AccountStore
from app.adapters.base import AdapterError
from app.scheduler.auto_scheduler import AccountRunner
from app.scheduler.models import OperationResult
from app.services import credentials
from suite.config import SchedulerSettings
from suite.runtime import Runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, '_HAS_KEYRING', False)
    credentials.clear_all()
    rt = Runtime(tmp_path / 'scheduler')
    rt.startup()
    yield rt
    rt.shutdown()
    credentials.clear_all()


@pytest_asyncio.fixture
async def suite_client(network, runtime):
    from dashboard.app import app
    app.state.scheduler = runtime
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://localhost') as client:
        yield client
    app.state.scheduler = None


async def add_account(client, **extra):
    response = await client.post('/api/scheduler/accounts', json={'name':'Primary', **extra})
    assert response.status_code == 200, response.text
    return response.json()['account_id']


async def add_server(client, aid, **extra):
    response = await client.post(f'/api/scheduler/accounts/{aid}/targets', json={
        'name':'Feeder', 'guild_id':'123456', 'channel_id':'234567', **extra})
    assert response.status_code == 200, response.text
    return response.json()['server_id']


async def test_pages_and_shared_server_picker(suite_client, network):
    response = await suite_client.get('/scheduler')
    assert response.status_code == 200
    assert str(network.feeder.id) in response.text
    assert 'Scheduler' in (await suite_client.get('/')).text
    assert (await suite_client.get('/customize')).status_code == 200


async def test_account_and_target_crud_persist(suite_client, runtime):
    aid = await add_account(suite_client, server_cooldown_min=121)
    sid = await add_server(suite_client, aid, cooldown_min=155, message='hello')
    response = await suite_client.put(f'/api/scheduler/accounts/{aid}/targets/{sid}', json={
        'name':'Renamed', 'guild_id':'123456', 'channel_id':'234567', 'cooldown_min':166})
    assert response.status_code == 200
    restored = AccountStore(runtime.store._path).get(aid)
    assert restored.server_cooldown_min == 121
    assert restored.servers[0].cooldown_min == 166
    assert restored.servers[0].name == 'Renamed'
    assert (await suite_client.delete(f'/api/scheduler/accounts/{aid}/targets/{sid}')).status_code == 200
    assert runtime.store.get(aid).servers == []
    assert (await suite_client.delete(f'/api/scheduler/accounts/{aid}')).status_code == 200
    assert runtime.store.list() == []


async def test_secret_never_returned_or_written_to_json(suite_client, runtime):
    token = 'SECRET-test-credential-do-not-return'
    aid = await add_account(suite_client, token=token)
    for endpoint in ['/api/scheduler', '/api/scheduler/export', '/scheduler']:
        response = await suite_client.get(endpoint)
        assert token not in response.text
    assert token not in runtime.store._path.read_text()
    status = (await suite_client.get('/api/scheduler')).json()['accounts'][0]
    assert status['has_credential']
    assert status['credential_storage'] == 'session_only'
    response = await suite_client.post('/api/scheduler/accounts', json={'name':'Bad','token':token,'server_cooldown_min':-5})
    assert response.status_code == 422
    assert token not in response.text
    await suite_client.put(f'/api/scheduler/accounts/{aid}', json={'name':'Primary','clear_token':True})
    assert credentials.get_credential(aid) is None


async def test_shared_targets_allowed_across_accounts_but_not_within_one(suite_client):
    first = await add_account(suite_client)
    second = await add_account(suite_client, name='Second')
    await add_server(suite_client, first)
    response = await suite_client.post(f'/api/scheduler/accounts/{second}/targets', json={
        'name':'Duplicate', 'guild_id':'123456', 'channel_id':'999999'})
    assert response.status_code == 200
    response=await suite_client.post(f'/api/scheduler/accounts/{second}/targets',json={
        'name':'Duplicate in same account','guild_id':'123456','channel_id':'999999'})
    assert response.status_code==409


async def test_simulation_runs_without_credentials_and_stops(suite_client, runtime):
    prefs = SchedulerSettings(enabled=True, dry_run=True, start_offset_min=0)
    assert (await suite_client.put('/api/scheduler/settings', json=prefs.model_dump())).status_code == 200
    aid = await add_account(suite_client)
    await add_server(suite_client, aid)
    response = await suite_client.post(f'/api/scheduler/accounts/{aid}/actions/start')
    assert response.status_code == 200
    for _ in range(60):
        if runtime.store.get(aid).servers[0].total_simulated:
            break
        await asyncio.sleep(.01)
    server = runtime.store.get(aid).servers[0]
    assert server.total_simulated == 1
    assert server.total_ok == 0
    assert server.next_run_at > datetime.now(timezone.utc)
    # Editing a live object would race a runner; this must be rejected.
    assert (await suite_client.put(f'/api/scheduler/accounts/{aid}', json={'name':'Changed'})).status_code == 409
    assert (await suite_client.put('/api/scheduler/settings', json=prefs.model_dump())).status_code == 409
    await suite_client.post('/api/scheduler/control/stop-all')
    for _ in range(60):
        if runtime.scheduler.wait_stopped(aid): break
        await asyncio.sleep(.01)
    assert runtime.scheduler.wait_stopped(aid)
    response = await suite_client.put('/api/scheduler/settings', json=prefs.model_copy(update={'dry_run':False}).model_dump())
    assert response.status_code == 200
    assert runtime.store.get(aid).servers[0].next_run_at is None
    assert not runtime.scheduler.status()[0]['is_running']


async def test_disabled_scheduler_blocks_start_and_unknown_actions(suite_client):
    aid = await add_account(suite_client)
    assert (await suite_client.post(f'/api/scheduler/accounts/{aid}/actions/start')).status_code == 409
    assert (await suite_client.post('/api/scheduler/control/start-all')).status_code == 409
    assert (await suite_client.post(f'/api/scheduler/accounts/{aid}/actions/unknown')).status_code == 409


async def test_import_atomic_disabled_and_roundtrip(suite_client, runtime):
    rows = [{'account_id':'legacy-1','name':'Old account','token_type':'user', 'auto_start':True,
             'token':'SHOULD-NOT-IMPORT', 'servers':[{'name':'Old server','guild_id':'77','channel_id':'88'}]}]
    response = await suite_client.post('/api/scheduler/import/accounts',json={'accounts':rows})
    assert response.status_code == 200
    account = runtime.store.get('legacy-1')
    assert not account.auto_start and not account.enabled
    assert credentials.get_credential('legacy-1') is None
    before = runtime.store._path.read_text()
    # The first entry is valid, the second is invalid: neither may be added.
    invalid = [{'name':'Valid first'}, {'name':'Bad second','servers':[{'name':'bad','guild_id':'abc','channel_id':'1'}]}]
    response = await suite_client.post('/api/scheduler/import/accounts',json={'accounts':invalid})
    assert response.status_code == 400
    assert runtime.store._path.read_text() == before
    exported = (await suite_client.get('/api/scheduler/export')).json()
    assert 'SHOULD-NOT-IMPORT' not in json.dumps(exported)
    assert 'browser_path' not in json.dumps(exported)
    await suite_client.delete('/api/scheduler/accounts/legacy-1')
    response = await suite_client.post('/api/scheduler/import/accounts',json={'accounts':exported['accounts']})
    assert response.status_code == 200
    assert runtime.store.get('legacy-1').servers[0].guild_id == '77'


async def test_appearance_validation_and_shared_branding(suite_client):
    response = await suite_client.post('/api/suite/appearance',json={'dashboard_title':'Ismail Control',
        'dashboard_theme':'light', 'dashboard_accent':'#21734f', 'dashboard_compact':True})
    assert response.status_code == 200
    page = (await suite_client.get('/scheduler')).text
    assert 'Ismail Control' in page and 'data-theme="light"' in page
    assert (await suite_client.post('/api/suite/appearance',json={'dashboard_accent':'red; display:none'})).status_code == 422


async def test_local_boundary_and_cross_site_writes(suite_client):
    assert (await suite_client.get('/scheduler',headers={'host':'attacker.example'})).status_code == 403
    assert (await suite_client.post('/api/scheduler/accounts',json={'name':'Cross-site'},headers={'origin':'https://attacker.example'})).status_code == 403
    assert (await suite_client.post('/api/scheduler/accounts',json={'name':'Good'},headers={'origin':'http://localhost'})).status_code == 200


def configured_runner(runtime, monkeypatch, *, token_type='bot', dry_run=False):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=dry_run,max_failures=2,retry_base_seconds=2,retry_max_seconds=5))
    account = Account(name='Test', token_type=token_type, account_cooldown_min=0,
                      servers=[ServerTarget(name='Feeder',guild_id='1',channel_id='2')])
    runtime.store.upsert(account)
    credentials.store_credential(account.account_id,'dummy-token')
    adapter = Mock()
    adapter.execute.return_value = OperationResult(success=True,verified=False,external_id='42')
    adapter.verify_result.return_value = OperationResult(success=True,verified=True)
    runner = AccountRunner(account,runtime.store,preferences=runtime.preferences.get)
    monkeypatch.setattr(runner,'_get_adapter',lambda kind:adapter)
    return runner, account, adapter


def test_exact_bot_delivery_verification(runtime, monkeypatch):
    runner, account, adapter = configured_runner(runtime,monkeypatch)
    runner._execute_server(account,account.servers[0])
    target=runtime.store.get(account.account_id).servers[0]
    assert target.total_ok==1 and target.last_result=='confirmed'
    adapter.verify_result.assert_called_once()
    adapter.disconnect.assert_called_once()


def test_user_delivery_is_never_claimed_confirmed(runtime, monkeypatch):
    runner, account, adapter = configured_runner(runtime,monkeypatch,token_type='user')
    adapter.execute.return_value=OperationResult(success=True,verified=True,external_id='42')
    runner._execute_server(account,account.servers[0])
    target=runtime.store.get(account.account_id).servers[0]
    assert target.total_ok==0 and target.total_sent==1
    assert target.last_result=='sent_unconfirmed'


def test_reservation_written_before_external_send(runtime, monkeypatch):
    runner, account, adapter = configured_runner(runtime,monkeypatch)
    def execute(*args):
        restored=AccountStore(runtime.store._path).get(account.account_id)
        assert restored.last_action_at is not None
        assert restored.servers[0].next_run_at > datetime.now(timezone.utc)
        assert restored.servers[0].last_result=='in_progress'
        return OperationResult(success=True)
    adapter.execute.side_effect=execute
    runner._execute_server(account,account.servers[0])
    assert runtime.store.get(account.account_id).servers[0].total_sent==1


def test_auth_failure_disables_target_without_leaking_body(runtime,monkeypatch):
    runner,account,adapter=configured_runner(runtime,monkeypatch)
    adapter.connect.side_effect=AdapterError('INVALID_TOKEN','SECRET RESPONSE',retryable=False)
    runner._execute_server(account,account.servers[0])
    target=runtime.store.get(account.account_id).servers[0]
    assert not target.enabled and target.total_fail==1
    assert 'SECRET RESPONSE' not in runtime.store._path.read_text()


def test_rate_limit_respects_server_wait(runtime,monkeypatch):
    runner,account,adapter=configured_runner(runtime,monkeypatch)
    adapter.execute.side_effect=AdapterError('RATE_LIMITED',retry_after_ms=120000)
    before=datetime.now(timezone.utc)
    runner._execute_server(account,account.servers[0])
    target=runtime.store.get(account.account_id).servers[0]
    assert target.next_run_at >= before+timedelta(seconds=120)
    assert target.enabled


def test_failure_threshold_disables_target(runtime,monkeypatch):
    runner,account,adapter=configured_runner(runtime,monkeypatch)
    adapter.execute.side_effect=AdapterError('NETWORK',retryable=True)
    runner._execute_server(account,account.servers[0])
    runner._execute_server(account,account.servers[0])
    assert not runtime.store.get(account.account_id).servers[0].enabled


def test_process_lock_blocks_second_runtime_and_releases(runtime):
    second=Runtime(runtime.directory)
    with pytest.raises(RuntimeError,match='Another dashboard'):
        second.startup()
    runtime.shutdown()
    second.startup()
    second.shutdown()


def test_store_snapshots_do_not_mutate_saved_configuration(runtime):
    account=Account(name='Original')
    runtime.store.upsert(account)
    runtime.store.get(account.account_id).name='Mutated'
    assert runtime.store.get(account.account_id).name=='Original'


def test_corrupt_store_is_not_overwritten(tmp_path):
    path=tmp_path/'accounts.json'
    path.write_text('{broken')
    with pytest.raises(ValueError):
        AccountStore(path)
    assert path.read_text()=='{broken'


def test_persisted_utc_timing_and_last_action_survive_restart(runtime):
    account=Account(name='Saved',last_action_at=datetime.now(timezone.utc),
                    servers=[ServerTarget(name='Server',guild_id='1',channel_id='2',
                             next_run_at=datetime.now(timezone.utc)+timedelta(hours=2))])
    runtime.store.upsert(account)
    restored=AccountStore(runtime.store._path).get(account.account_id)
    assert restored.last_action_at.tzinfo is not None
    assert restored.servers[0].next_run_at>datetime.now(timezone.utc)


def test_start_twice_creates_one_worker(runtime):
    account=Account(name='Single')
    runtime.store.upsert(account)
    runtime.scheduler.start_account(account.account_id)
    first=runtime.scheduler._runners[account.account_id]
    runtime.scheduler.start_account(account.account_id)
    assert runtime.scheduler._runners[account.account_id] is first


def test_auto_start_restart_does_not_repeat_reserved_run(runtime):
    import time
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True,start_offset_min=0))
    account=Account(name='Restart',auto_start=True,servers=[ServerTarget(name='Saved',guild_id='1',channel_id='2')])
    runtime.store.upsert(account)
    runtime.scheduler.start(auto_only=True)
    for _ in range(100):
        if runtime.store.get(account.account_id).servers[0].total_simulated: break
        time.sleep(.005)
    runtime.shutdown()
    previous=runtime.store.get(account.account_id).servers[0]
    assert previous.total_simulated==1
    restarted=Runtime(runtime.directory)
    try:
        restarted.startup()
        time.sleep(.03)
        current=restarted.store.get(account.account_id).servers[0]
        assert current.total_simulated==1
        assert current.next_run_at==previous.next_run_at
        assert restarted.scheduler.status()[0]['is_running']
    finally:
        restarted.shutdown()
