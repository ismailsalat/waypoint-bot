"""Analytics.

These are observed counts, nothing more. The dashboard never claims a tag or a
message version caused a change; it just shows what happened.

Everything here counts production activity only. Member joins, DM events and
conversions marked is_test — anything recorded while development mode was on,
and every test send from the message studio — are excluded from every figure
on this page, so testing can never move your numbers.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import constants
from database.database import as_utc
from database.models import Conversion, DMEvent, MemberJoin, MessageVersion, Server, TagExperiment


def rate(part: int, whole: int) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


async def _feeder_names(db: AsyncSession) -> dict[int, str]:
    rows = (await db.execute(select(Server))).scalars().all()
    return {row.guild_id: row.name for row in rows}


async def feeder_rows(db: AsyncSession) -> list[dict[str, Any]]:
    feeders = (
        await db.execute(select(Server).where(Server.server_type == constants.FEEDER).order_by(Server.name))
    ).scalars().all()

    joins: dict[int, int] = defaultdict(int)
    stmt = select(MemberJoin.guild_id).where(MemberJoin.is_test.is_(False))
    for guild_id, in await db.execute(stmt):
        joins[guild_id] += 1

    dms: dict[int, int] = defaultdict(int)
    stmt = select(DMEvent.feeder_guild_id).where(
        DMEvent.status == constants.DM_SENT, DMEvent.is_test.is_(False)
    )
    for guild_id, in await db.execute(stmt):
        if guild_id:
            dms[guild_id] += 1

    conversions: dict[int, int] = defaultdict(int)
    stmt = select(Conversion.source_guild_id).where(
        Conversion.attribution == constants.ATTR_INVITE, Conversion.is_test.is_(False)
    )
    for guild_id, in await db.execute(stmt):
        if guild_id:
            conversions[guild_id] += 1

    rows = []
    for feeder in feeders:
        j = joins.get(feeder.guild_id, 0)
        rows.append(
            {
                "guild_id": feeder.guild_id,
                "name": feeder.name,
                "joins": j,
                "dms": dms.get(feeder.guild_id, 0),
                "conversions": conversions.get(feeder.guild_id, 0),
                "rate": rate(conversions.get(feeder.guild_id, 0), j),
                "bot_present": feeder.bot_present,
            }
        )
    return rows


async def network_summary(db: AsyncSession) -> dict[str, Any]:
    rows = await feeder_rows(db)
    total_joins = sum(r["joins"] for r in rows)
    attributed = sum(r["conversions"] for r in rows)

    all_conversions = (
        await db.execute(select(Conversion).where(Conversion.is_test.is_(False)))
    ).scalars().all()
    unknown = sum(1 for c in all_conversions if c.attribution != constants.ATTR_INVITE)

    return {
        "feeders": len(rows),
        "feeder_joins": total_joins,
        "conversions": attributed,
        "unattributed": unknown,
        "total_main_joins": len(all_conversions),
        "rate": rate(attributed, total_joins),
        "rows": rows,
    }


async def tag_stats(db: AsyncSession) -> list[dict[str, Any]]:
    """Per-tag observed joins and conversions, summed over every experiment
    window in which that tag was live."""
    experiments = (await db.execute(select(TagExperiment))).scalars().all()
    joins = (
        await db.execute(select(MemberJoin).where(MemberJoin.is_test.is_(False)))
    ).scalars().all()
    conversions = (
        await db.execute(select(Conversion).where(Conversion.is_test.is_(False)))
    ).scalars().all()

    joins_by_guild: dict[int, list[MemberJoin]] = defaultdict(list)
    for join in joins:
        joins_by_guild[join.guild_id].append(join)

    conv_by_experiment: dict[int, int] = defaultdict(int)
    for conv in conversions:
        if conv.tag_experiment_id and conv.attribution == constants.ATTR_INVITE:
            conv_by_experiment[conv.tag_experiment_id] += 1

    tag_joins: dict[str, int] = defaultdict(int)
    tag_conversions: dict[str, int] = defaultdict(int)

    for exp in experiments:
        start = as_utc(exp.started_at)
        end = as_utc(exp.ended_at)
        window_joins = 0
        for join in joins_by_guild.get(exp.feeder_guild_id, []):
            joined = as_utc(join.joined_at)
            if joined is None or start is None:
                continue
            if joined >= start and (end is None or joined < end):
                window_joins += 1
        for tag in exp.tags or []:
            tag_joins[tag] += window_joins
            tag_conversions[tag] += conv_by_experiment.get(exp.id, 0)

    out = []
    for tag in sorted(set(tag_joins) | set(tag_conversions)):
        out.append(
            {
                "tag": tag,
                "joins": tag_joins.get(tag, 0),
                "conversions": tag_conversions.get(tag, 0),
                "rate": rate(tag_conversions.get(tag, 0), tag_joins.get(tag, 0)),
            }
        )
    out.sort(key=lambda r: r["joins"], reverse=True)
    return out


async def message_stats(db: AsyncSession) -> list[dict[str, Any]]:
    versions = (
        await db.execute(select(MessageVersion).where(MessageVersion.kind == constants.KIND_DM))
    ).scalars().all()

    delivered: dict[int, int] = defaultdict(int)
    stmt = select(DMEvent.message_version_id).where(
        DMEvent.status == constants.DM_SENT, DMEvent.is_test.is_(False)
    )
    for version_id, in await db.execute(stmt):
        if version_id:
            delivered[version_id] += 1

    converted: dict[int, int] = defaultdict(int)
    stmt = select(Conversion.message_version_id).where(Conversion.is_test.is_(False))
    for version_id, in await db.execute(stmt):
        if version_id:
            converted[version_id] += 1

    names = await _feeder_names(db)
    rows = []
    for version in versions:
        d = delivered.get(version.id, 0)
        c = converted.get(version.id, 0)
        if d == 0 and c == 0 and version.status == constants.DRAFT:
            continue
        label = f"Version {version.version}"
        if version.scope == constants.SCOPE_FEEDER:
            label += f" ({names.get(version.guild_id or 0, 'feeder')} override)"
        rows.append(
            {
                "id": version.id,
                "label": label,
                "status": version.status,
                "delivered": d,
                "conversions": c,
                "rate": rate(c, d),
            }
        )
    rows.sort(key=lambda r: r["id"])
    return rows


async def cross_membership(db: AsyncSession) -> list[dict[str, Any]]:
    """Overlap between feeders. Analytics only, never used for attribution."""
    joins = (
        await db.execute(select(MemberJoin).where(MemberJoin.is_test.is_(False)))
    ).scalars().all()
    names = await _feeder_names(db)
    feeders = {
        row.guild_id
        for row in (
            await db.execute(select(Server).where(Server.server_type == constants.FEEDER))
        ).scalars().all()
    }

    members: dict[int, set[int]] = defaultdict(set)
    for join in joins:
        if join.guild_id in feeders:
            members[join.guild_id].add(join.user_id)

    out = []
    guild_ids = sorted(members)
    for i, a in enumerate(guild_ids):
        for b in guild_ids[i + 1:]:
            shared = len(members[a] & members[b])
            if not shared:
                continue
            out.append(
                {
                    "a": names.get(a, str(a)),
                    "b": names.get(b, str(b)),
                    "shared": shared,
                    "pct_of_a": rate(shared, len(members[a])),
                    "pct_of_b": rate(shared, len(members[b])),
                }
            )
    out.sort(key=lambda r: r["shared"], reverse=True)
    return out[:20]


async def dm_breakdown(db: AsyncSession) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    stmt = select(DMEvent.status).where(DMEvent.is_test.is_(False))
    for status, in await db.execute(stmt):
        counts[status] += 1
    return [{"status": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
