"""Multiple user-token accounts, independent timers and live feeder setup."""
from __future__ import annotations
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
import pytest
from app.accounts.models import Account,ServerTarget
from app.scheduler.auto_scheduler import AccountRunner
from app.scheduler.models import OperationResult
from app.services import credentials
from suite.config import SchedulerSettings
from suite.bridge import refresh_catalog
from database import crud
from core import constants
from tests.test_suite_integration import runtime,suite_client,add_account,add_server


def target(gid='1',**extra):
    return ServerTarget(name='Feeder '+gid,guild_id=gid,channel_id='9',**extra)


def wait_for(condition):
    for _ in range(200):
        if condition():return
        time.sleep(.01)
    assert condition()


def test_many_accounts_run_their_own_queues(runtime):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True,start_offset_min=0))
    accounts=[Account(name=f'Account {n}',token_type='user',servers=[target(str(n))]) for n in range(1,9)]
    for a in accounts:runtime.store.upsert(a)
    runtime.scheduler.start(auto_only=False)
    wait_for(lambda:all(a.servers[0].total_simulated==1 for a in runtime.store.list()))
    assert len([s for s in runtime.scheduler.status() if s['is_running']])==8
    assert all(a.account_cooldown_min==30 and a.server_cooldown_min==120 for a in runtime.store.list())


def test_parallel_accounts_share_server_cooldown(runtime,monkeypatch):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=False,start_offset_min=0))
    calls=[]
    def execute(*args):
        calls.append(args)
        time.sleep(.05)
        return OperationResult(success=True,verified=False)
    accounts=[Account(name=f'Account {n}',token_type='user',servers=[target()]) for n in range(2)]
    runners=[]
    for account in accounts:
        runtime.store.upsert(account)
        credentials.store_credential(account.account_id,'test-only')
        adapter=Mock();adapter.execute.side_effect=execute
        runner=AccountRunner(account,runtime.store,preferences=runtime.preferences.get,cooldowns=runtime.cooldowns)
        monkeypatch.setattr(runner,'_get_adapter',lambda kind,adapter=adapter:adapter)
        runners.append(runner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(r._execute_server,a,a.servers[0]) for r,a in zip(runners,accounts)]
        for f in futures:f.result()
    assert len(calls)==1
    saved=runtime.store.list()
    assert sum(a.servers[0].total_sent for a in saved)==1
    assert any(a.servers[0].last_result=='waiting_shared_cooldown' for a in saved)
    assert all(a.servers[0].next_run_at>datetime.now(timezone.utc)+timedelta(minutes=119) for a in saved)


def test_custom_account_delay_keeps_second_server_waiting(runtime):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True,start_offset_min=0))
    account=Account(name='Timing',account_cooldown_min=.02,server_cooldown_min=150,servers=[target('1'),target('2')])
    runtime.store.upsert(account)
    runtime.scheduler.start_account(account.account_id)
    wait_for(lambda:runtime.store.get(account.account_id).servers[0].total_simulated==1)
    first=runtime.store.get(account.account_id)
    assert first.servers[1].total_simulated==0
    wait_for(lambda:runtime.store.get(account.account_id).servers[1].total_simulated==1)
    saved=runtime.store.get(account.account_id)
    assert (saved.servers[1].last_run_at-saved.servers[0].last_run_at).total_seconds()>=1.2
    assert saved.servers[0].next_run_at>=saved.servers[0].last_run_at+timedelta(minutes=150)


def test_server_override_is_independent_of_account_delay(runtime):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True))
    account=Account(name='Overrides',account_cooldown_min=11,server_cooldown_min=123,servers=[target(cooldown_min=75)])
    runtime.store.upsert(account)
    runner=AccountRunner(account,runtime.store,preferences=runtime.preferences.get)
    runner._execute_server(account,account.servers[0])
    saved=runtime.store.get(account.account_id)
    assert saved.account_cooldown_min==11
    assert saved.server_cooldown_min==123
    assert saved.servers[0].next_run_at-saved.servers[0].last_run_at==timedelta(minutes=75)


def test_rebuilding_one_feeder_does_not_block_other_targets(runtime):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True,start_offset_min=0))
    account=Account(name='Setup in progress',account_cooldown_min=0,servers=[target('1',follow_managed_channel=True),target('2')])
    runtime.store.upsert(account)
    runtime.update_catalog({'1':{'ready':False,'channel_id':'9'}})
    runtime.scheduler.start_account(account.account_id)
    wait_for(lambda:runtime.store.get(account.account_id).servers[1].total_simulated==1)
    assert runtime.store.get(account.account_id).servers[0].total_simulated==0
    runtime.update_catalog({'1':{'ready':True,'channel_id':'NEW'}})
    wait_for(lambda:runtime.store.get(account.account_id).servers[0].total_simulated==1)
    assert runtime.store.get(account.account_id).servers[0].channel_id=='NEW'


async def test_pending_rebuild_blocks_managed_target_then_follows_new_channel(network,runtime):
    feeder=await crud.get_server(network.db,network.feeder.id)
    feeder.bump_channel_id=111
    await network.db.commit()
    await refresh_catalog(runtime)
    assert runtime.target_status(str(feeder.guild_id))=={'ready':True,'channel_id':'111'}
    job=await crud.queue_task(network.db,constants.TASK_FRESH_SETUP,{'guild_id':feeder.guild_id})
    await refresh_catalog(runtime)
    assert not runtime.target_status(str(feeder.guild_id))['ready']
    job.status=constants.TASK_DONE
    feeder.bump_channel_id=222
    await network.db.commit()
    await refresh_catalog(runtime)
    assert runtime.target_status(str(feeder.guild_id))=={'ready':True,'channel_id':'222'}


async def test_user_token_defaults_and_both_timers_customizable(suite_client,runtime):
    aid=await add_account(suite_client,account_cooldown_min=7.5,server_cooldown_min=95)
    sid=await add_server(suite_client,aid,cooldown_min=65)
    account=runtime.store.get(aid)
    assert account.token_type=='user'
    assert account.account_cooldown_min==7.5 and account.server_cooldown_min==95
    assert account.servers[0].cooldown_min==65
    response=await suite_client.put('/api/scheduler/settings',json=SchedulerSettings(
        default_account_cooldown_min=12,default_server_cooldown_min=80).model_dump())
    assert response.status_code==200
    assert runtime.preferences.get()['default_account_cooldown_min']==12
    assert runtime.preferences.get()['default_server_cooldown_min']==80


async def test_feeder_routing_can_be_edited_while_bump_account_runs(suite_client,runtime,network):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True,start_offset_min=0))
    aid=await add_account(suite_client)
    await add_server(suite_client,aid)
    await suite_client.post(f'/api/scheduler/accounts/{aid}/actions/start')
    response=await suite_client.post(f'/feeders/{network.feeder.id}/destinations',data={
        'destination_mode':'selected','destination_guild_ids':str(network.main.id)})
    assert response.status_code==303
    assert runtime.scheduler.status()[0]['is_running']
    async with __import__('database.database',fromlist=['session']).session() as db:
        saved=await crud.get_server(db,network.feeder.id)
        assert saved.destination_guild_ids==[network.main.id]


def test_leaving_simulation_clears_shared_waits(runtime):
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True))
    accounts=[Account(name=str(n),servers=[target()]) for n in range(2)]
    for account in accounts:
        runtime.store.upsert(account)
        AccountRunner(account,runtime.store,preferences=runtime.preferences.get,cooldowns=runtime.cooldowns)._execute_server(account,account.servers[0])
    assert runtime.store.get(accounts[1].account_id).servers[0].last_result=='simulated_shared_cooldown'
    runtime.save_settings(SchedulerSettings(enabled=True,dry_run=False))
    assert all(a.servers[0].next_run_at is None for a in runtime.store.list())


async def test_target_can_be_assigned_before_managed_channel_exists(suite_client,runtime):
    aid=await add_account(suite_client)
    response=await suite_client.post(f'/api/scheduler/accounts/{aid}/targets',json={
        'name':'Awaiting setup','guild_id':'123456','channel_id':'','follow_managed_channel':True})
    assert response.status_code==200
    runtime.preferences.save(SchedulerSettings(enabled=True,dry_run=True,start_offset_min=0))
    runtime.update_catalog({'123456':{'ready':False,'channel_id':''}})
    await suite_client.post(f'/api/scheduler/accounts/{aid}/actions/start')
    await asyncio.sleep(.03)
    assert runtime.store.get(aid).servers[0].total_simulated==0
    runtime.update_catalog({'123456':{'ready':True,'channel_id':'987654'}})
    for _ in range(150):
        if runtime.store.get(aid).servers[0].total_simulated: break
        await asyncio.sleep(.01)
    assert runtime.store.get(aid).servers[0].channel_id=='987654'
