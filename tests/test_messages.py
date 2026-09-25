"""Message rendering, versions and per-feeder overrides."""
from __future__ import annotations

from core import constants, rendering
from database import crud


def test_variables_are_substituted():
    context = rendering.build_context(
        feeder_name="Rewind", main_server_name="Side Quest", user_name="alex"
    )
    text = rendering.render_text("Thanks for joining {feeder_name}, {user_name}!", context)
    assert text == "Thanks for joining Rewind, alex!"


def test_unknown_variables_are_left_alone_and_reported():
    context = rendering.build_context(feeder_name="Rewind")
    assert rendering.render_text("Hi {not_a_variable}", context) == "Hi {not_a_variable}"
    assert rendering.unknown_variables({"body": "Hi {not_a_variable}"}) == ["not_a_variable"]


def test_render_content_covers_every_text_field():
    context = rendering.build_context(main_server_name="Side Quest", network_name="SQ Network")
    rendered = rendering.render_content(
        {
            "body": "Join {main_server_name}",
            "button_label": "Join {main_server_name}",
            "footer": "{network_name}",
            "use_embed": True,
        },
        context,
    )
    assert rendered["body"] == "Join Side Quest"
    assert rendered["button_label"] == "Join Side Quest"
    assert rendered["footer"] == "SQ Network"
    assert rendered["use_embed"] is True


async def test_draft_does_not_change_production(network):
    db = network.db
    first = await crud.save_draft(
        db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "version one"}
    )
    await crud.publish_version(db, first.id)

    draft = await crud.save_draft(
        db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "version two"}
    )
    content, version_id = await crud.message_content(db, constants.KIND_DM)
    assert content["body"] == "version one"
    assert version_id == first.id

    await crud.publish_version(db, draft.id)
    content, version_id = await crud.message_content(db, constants.KIND_DM)
    assert content["body"] == "version two"
    assert version_id == draft.id

    await db.refresh(first)
    assert first.status == constants.ARCHIVED


async def test_feeder_override_beats_the_global_message(network):
    db = network.db
    global_version = await crud.save_draft(
        db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "global text"}
    )
    await crud.publish_version(db, global_version.id)

    override = await crud.save_draft(
        db, constants.KIND_DM, constants.SCOPE_FEEDER, {"body": "rewind text"}, network.feeder.id
    )
    await crud.publish_version(db, override.id)

    content, _ = await crud.message_content(db, constants.KIND_DM, network.feeder.id)
    assert content["body"] == "rewind text"

    other_feeder = await crud.upsert_server(db, 424242, "Rivals HQ", network.owner_id, constants.FEEDER)
    content, _ = await crud.message_content(db, constants.KIND_DM, other_feeder.guild_id)
    assert content["body"] == "global text"


async def test_old_versions_are_kept_and_restorable(network):
    db = network.db
    v1 = await crud.save_draft(db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "one"})
    await crud.publish_version(db, v1.id)
    v2 = await crud.save_draft(db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "two"})
    await crud.publish_version(db, v2.id)

    before = await crud.list_versions(db, constants.KIND_DM, constants.SCOPE_GLOBAL)
    restored = await crud.restore_version(db, v1.id)
    assert restored.content["body"] == "one"
    assert restored.version == v2.version + 1
    assert restored.status == constants.DRAFT

    versions = await crud.list_versions(db, constants.KIND_DM, constants.SCOPE_GLOBAL)
    assert len(versions) == len(before) + 1  # nothing was overwritten

    await crud.publish_version(db, restored.id)
    content, _ = await crud.message_content(db, constants.KIND_DM)
    assert content["body"] == "one"


async def test_build_payload_uses_a_link_button():
    from bot import messages

    payload = messages.build_payload(
        {"body": "hello", "button_label": "Join", "use_embed": False}, "https://discord.gg/abc"
    )
    assert payload["content"] == "hello"
    assert payload["embed"] is None
    button = payload["view"].children[0]
    assert button.url == "https://discord.gg/abc"
    assert button.label == "Join"


async def test_build_payload_supports_embeds():
    from bot import messages

    payload = messages.build_payload(
        {
            "body": "hello",
            "use_embed": True,
            "embed_title": "Side Quest",
            "embed_color": "#ff0000",
            "footer": "network",
            "button_label": "Join",
        },
        "https://discord.gg/abc",
    )
    assert payload["content"] is None
    assert payload["embed"].title == "Side Quest"
    assert payload["embed"].description == "hello"
    assert payload["embed"].colour.value == 0xFF0000
    assert payload["embed"].footer.text == "network"
