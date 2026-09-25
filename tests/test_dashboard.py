"""Dashboard pages and the main-server switch."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from core import constants, settings as settings_store
from database import crud


@pytest_asyncio.fixture
async def client(network):
    from dashboard.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


@pytest.mark.parametrize(
    "path",
    ["/", "/servers", "/feeders", "/messages", "/tags", "/conversions", "/analytics", "/health", "/settings"],
)
async def test_every_page_loads(client, path):
    response = await client.get(path)
    assert response.status_code == 200, path


async def test_the_first_run_screen_appears_before_setup(engine):
    from dashboard.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"


async def test_the_preview_uses_the_real_renderer(client, network):
    response = await client.post(
        "/api/preview",
        json={
            "content": {"body": "Thanks for joining {feeder_name}!", "button_label": "Join {main_server_name}"},
            "preview_guild_id": str(network.feeder.id),
        },
    )
    data = response.json()
    assert data["body"] == "Thanks for joining Rewind!"
    assert data["button_label"] == "Join Side Quest"


async def test_saving_a_draft_then_publishing_it(client, network):
    response = await client.post(
        "/messages/draft",
        data={"kind": constants.KIND_DM, "guild_id": "", "body": "brand new text", "button_label": "Join"},
    )
    assert response.status_code == 303

    versions = await crud.list_versions(network.db, constants.KIND_DM, constants.SCOPE_GLOBAL)
    draft = versions[0]
    assert draft.status == constants.DRAFT

    content, _ = await crud.message_content(network.db, constants.KIND_DM)
    assert content["body"] != "brand new text"  # still the old published text

    await client.post(f"/messages/{draft.id}/publish")
    content, _ = await crud.message_content(network.db, constants.KIND_DM)
    assert content["body"] == "brand new text"


async def test_changing_the_main_server_keeps_history(client, network):
    await crud.record_conversion(
        network.db, 1, "old", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    new_main = await crud.upsert_server(
        network.db, 888001, "New Main", network.owner_id, constants.DISABLED
    )

    response = await client.post(
        "/settings",
        data={
            "network_name": "Side Quest Network",
            "main_guild_id": str(new_main.guild_id),
            "default_funnel_channel_name": "join-main",
            "default_bump_channel_name": "bump",
            "default_dm_delay_seconds": "5",
            "default_funnel_mode": constants.LIVE,
            "dm_policy": constants.ONCE_PER_FEEDER,
            "dm_cooldown_days": "30",
            "failure_retry_days": "7",
            "repair_interval_minutes": "30",
            "bump_staff_role_names": "Staff",
        },
    )
    assert response.status_code == 303

    assert await settings_store.main_guild_id(network.db) == new_main.guild_id
    conversions = await crud.list_conversions(network.db)
    assert len(conversions) == 1
    assert conversions[0].destination_guild_id == network.main.id  # history untouched

    feeder = await crud.get_server(network.db, network.feeder.id)
    assert feeder.destination_guild_id == new_main.guild_id
    tasks = await crud.pending_tasks(network.db)
    assert any(task.task_type == constants.TASK_REPAIR for task in tasks)


async def test_changing_a_server_type_queues_setup(client, network):
    test_server = await crud.upsert_server(
        network.db, 999001, "Test Server", network.owner_id, constants.DISABLED
    )
    await client.post(f"/servers/{test_server.guild_id}/type", data={"server_type": constants.FEEDER})

    await network.db.refresh(test_server)
    assert test_server.server_type == constants.FEEDER
    tasks = await crud.pending_tasks(network.db)
    assert any(
        task.task_type == constants.TASK_SETUP_FEEDER
        and task.payload.get("guild_id") == test_server.guild_id
        for task in tasks
    )
