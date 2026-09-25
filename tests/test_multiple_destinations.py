"""End-to-end routing from a feeder to one or several main communities."""
from __future__ import annotations
from types import SimpleNamespace
import re
import discord
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from core import routing, settings, constants
from database import crud
from database.database import session
from bot import feeder_setup, funnel_dm, messages
from bot.member_events import MemberEvents
from bot.invite_tracker import InviteTracker
from tests.conftest import FakeGuild, FakeUser, FakeBot
from tests.test_worker_and_commands import FakeInteraction

@pytest_asyncio.fixture
async def multi(network):
    second=FakeGuild('Community Two',network.owner_id)
    second.add_channel('general')
    third=FakeGuild('Community Three',network.owner_id)
    third.add_channel('general')
    network.bot.guilds.extend([second,third])
    for guild in (second,third):
        await crud.upsert_server(network.db,guild.id,guild.name,guild.owner_id,constants.DESTINATION)
    feeder=await crud.get_server(network.db,network.feeder.id)
    feeder.destination_guild_ids=[network.main.id,second.id]
    await network.db.commit()
    return SimpleNamespace(**vars(network),second=second,third=third,feeder_row=feeder)

@pytest_asyncio.fixture
async def multi_client(multi):
    from dashboard.app import app
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://localhost') as client:
        yield client

async def test_setup_creates_one_invite_and_button_per_selected_main(multi):
    report=await feeder_setup.ensure_feeder(multi.bot,multi.db,multi.feeder)
    assert report['errors']==[]
    rows=await routing.rows(multi.db,multi.feeder_row)
    assert len(rows)==2 and all(d['invite_url'] for d in rows)
    assert len({d['invite_url'] for d in rows})==2
    channel=multi.feeder.get_channel(multi.feeder_row.funnel_channel_id)
    message=channel.messages[multi.feeder_row.funnel_message_id]
    buttons=message.payload['view'].children
    assert [b.label for b in buttons]==['Join Side Quest','Join Community Two']
    assert {b.url for b in buttons}=={d['invite_url'] for d in rows}
    assert 'Community Three' not in message.payload['embed'].description

async def test_two_feeders_can_route_to_different_main_servers(multi):
    other=FakeGuild('Other feeder',multi.owner_id)
    multi.bot.guilds.append(other)
    row=await crud.upsert_server(multi.db,other.id,other.name,other.owner_id,constants.FEEDER)
    row.destination_guild_ids=[multi.third.id]
    await multi.db.commit()
    await feeder_setup.ensure_feeder(multi.bot,multi.db,other)
    user=FakeUser()
    assert await funnel_dm.handle_feeder_join(multi.bot,user,other.id,wait=False)==constants.DM_SENT
    assert len(user.sent[0]['view'].children)==1
    assert user.sent[0]['view'].children[0].label=='Join Community Three'
    assert 'Side Quest' not in user.sent[0]['content']

async def test_one_dm_offers_multiple_selected_destinations(multi):
    user=FakeUser()
    assert await funnel_dm.handle_feeder_join(multi.bot,user,multi.feeder.id,wait=False)==constants.DM_SENT
    assert len(user.sent)==1
    assert len(user.sent[0]['view'].children)==2
    assert await funnel_dm.handle_feeder_join(multi.bot,user,multi.feeder.id,wait=False)==constants.DM_SKIPPED_POLICY
    assert len(user.sent)==1

async def test_dm_skips_joined_destinations_but_keeps_others(multi):
    user=FakeUser()
    multi.main.members[user.id]=user
    assert await funnel_dm.handle_feeder_join(multi.bot,user,multi.feeder.id,wait=False)==constants.DM_SENT
    assert [b.label for b in user.sent[0]['view'].children]==['Join Community Two']
    assert 'Side Quest' not in user.sent[0]['content']

async def test_no_dm_when_member_belongs_to_every_destination(multi):
    user=FakeUser()
    multi.main.members[user.id]=user
    multi.second.members[user.id]=user
    assert await funnel_dm.handle_feeder_join(multi.bot,user,multi.feeder.id,wait=False)==constants.DM_SKIPPED_IN_MAIN
    assert user.sent==[]

async def test_explicit_destinations_work_without_a_global_default(multi):
    await settings.set_many(multi.db,{'main_guild_id':None})
    report=await feeder_setup.ensure_feeder(multi.bot,multi.db,multi.feeder)
    assert report['errors']==[]
    assert len(report['destinations'])==2

async def test_default_change_preserves_pinned_destinations(multi):
    selected=list(multi.feeder_row.destination_guild_ids)
    await crud.set_main_server(multi.db,multi.third.id)
    assert await routing.destination_ids(multi.db,multi.feeder_row)==selected
    assert (await crud.get_server(multi.db,multi.main.id)).server_type==constants.DESTINATION

async def test_dashboard_saves_multiple_destinations_and_rejects_feeder_target(multi_client,multi):
    url=f'/feeders/{multi.feeder.id}/destinations'
    response=await multi_client.post(url,data={'destination_mode':'selected','destination_guild_ids':[str(multi.second.id),str(multi.third.id)]})
    assert response.status_code==303
    async with session() as db:
        row=await crud.get_server(db,multi.feeder.id)
        assert await routing.destination_ids(db,row)==[multi.second.id,multi.third.id]
    response=await multi_client.post(url,data={'destination_mode':'selected','destination_guild_ids':[str(multi.feeder.id)]})
    assert 'Destinations' in response.headers['location'] or 'destinations' in response.headers['location']
    page=await multi_client.get(f'/feeders/{multi.feeder.id}')
    assert 'Community Two' in page.text and 'Community Three' in page.text
    assert 'Save destinations' in page.text

async def test_reconfiguring_replaces_welcome_buttons_and_old_callback_is_safe(multi):
    await feeder_setup.ensure_feeder(multi.bot,multi.db,multi.feeder)
    old_button=messages.DestinationButton(multi.feeder.id,multi.second.id,'Two',discord.ButtonStyle.primary)
    multi.feeder_row.destination_guild_ids=[multi.third.id]
    await multi.db.commit()
    await feeder_setup.ensure_feeder(multi.bot,multi.db,multi.feeder)
    channel=multi.feeder.get_channel(multi.feeder_row.funnel_channel_id)
    view=channel.messages[multi.feeder_row.funnel_message_id].payload['view']
    assert [b.label for b in view.children]==['Join Community Three']
    interaction=FakeInteraction(multi.feeder,1)
    await old_button.callback(interaction)
    assert interaction.response.deferred
    assert 'no longer selected' in interaction.said()
    assert 'discord.gg/' not in interaction.said()

async def test_dynamic_destination_button_restores_and_uses_rotated_pair(multi):
    await feeder_setup.ensure_feeder(multi.bot,multi.db,multi.feeder)
    current=await crud.save_invite(multi.db,multi.feeder.id,multi.second.id,'ROTATED_TWO','https://discord.gg/ROTATED_TWO')
    item=discord.ui.Button(label='Two',style=discord.ButtonStyle.primary)
    match=re.fullmatch(r'waypoint:destination:(?P<feeder_id>\d+):(?P<target_id>\d+)',f'waypoint:destination:{multi.feeder.id}:{multi.second.id}')
    restored=await messages.DestinationButton.from_custom_id(None,item,match)
    interaction=FakeInteraction(multi.feeder,1)
    await restored.callback(interaction)
    assert current.url in interaction.said() and 'Community Two' in interaction.said()
    assert 'Side Quest' not in interaction.said()
    assert interaction.response.deferred

async def test_fresh_setup_checks_all_destinations_before_deleting(multi):
    old=multi.feeder.add_channel('important')
    multi.second.me.guild_permissions.create_instant_invite=False
    report=await feeder_setup.fresh_setup(multi.bot,multi.db,multi.feeder)
    assert report['errors'] and not old.deleted

async def test_additional_main_is_never_fresh_rebuilt(multi,multi_client):
    channel=multi.second.text_channels[0]
    report=await feeder_setup.fresh_setup(multi.bot,multi.db,multi.second)
    assert report['errors'] and not channel.deleted
    response=await multi_client.post(f'/feeders/{multi.second.id}/fresh',data={'confirm':'FRESH'})
    assert 'disabled' in response.headers['location'].lower()

async def test_additional_main_joins_are_attributed_to_correct_feeder(multi):
    await feeder_setup.ensure_feeder(multi.bot,multi.db,multi.feeder)
    tracker=InviteTracker(multi.bot)
    await tracker.prime(multi.second)
    invite=await crud.active_invite(multi.db,multi.feeder.id,multi.second.id)
    next(i for i in multi.second.invite_objects if i.code==invite.code).uses+=1
    member=FakeUser()
    member.guild=multi.second
    multi.bot.get_cog=lambda name:tracker if name=='InviteTracker' else None
    await MemberEvents(multi.bot).on_member_join(member)
    records=await crud.list_conversions(multi.db)
    assert records[0].destination_guild_id==multi.second.id
    assert records[0].source_guild_id==multi.feeder.id

async def test_three_main_servers_are_primed_on_ready(multi):
    tracker=InviteTracker(multi.bot)
    await tracker.on_ready()
    assert {multi.main.id,multi.second.id,multi.third.id}<=set(tracker.cache)

async def test_in_use_destination_cannot_become_feeder(multi_client,multi):
    response=await multi_client.post(f'/servers/{multi.second.id}/type',data={'server_type':'FEEDER'})
    assert 'feeders' in response.headers['location']
    await multi.db.refresh(await crud.get_server(multi.db,multi.second.id))
    assert (await crud.get_server(multi.db,multi.second.id)).server_type==constants.DESTINATION

async def test_preview_shows_one_custom_label_per_destination(multi_client,multi):
    response=await multi_client.post('/api/preview',json={'preview_guild_id':str(multi.feeder.id),
        'content':{'body':'Choose {main_server_name}','button_label':'Visit {main_server_name}'}})
    assert response.status_code==200
    assert [b['label'] for b in response.json()['buttons']]==['Visit Side Quest','Visit Community Two']


async def test_setup_command_does_not_demote_an_additional_main(multi):
    from bot.commands import WaypointCommands
    from core.config import config
    old=config.owner_user_ids
    config.owner_user_ids={multi.owner_id}
    try:
        interaction=FakeInteraction(multi.second,multi.owner_id)
        cog=WaypointCommands(multi.bot)
        await cog.setup_command.callback(cog,interaction)
        async with session() as db:
            assert (await crud.get_server(db,multi.second.id)).server_type==constants.DESTINATION
    finally:
        config.owner_user_ids=old

async def test_many_main_names_stay_within_message_limits(multi):
    from core import rendering
    content=rendering.render_content({'use_embed':True,'embed_title':'{main_server_name}','body':'{main_server_name}',
        'footer':'F'*2500},rendering.build_context(main_server_name='Long Community '*250))
    assert len(content['embed_title'])<=256
    assert len(content['footer'])<=2048
    assert len(content['embed_title'])+len(content['body'])+len(content['footer'])<=6000
    assert content['truncated_fields']
