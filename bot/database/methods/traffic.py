from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import delete, desc, func, select

from bot.database import Database
from bot.database.models.main import BoughtGoods, TrafficCampaign, TrafficClick, User
from bot.misc.timezone import MOSCOW_TZ


def _clean_slug(raw: str) -> str:
    """Sanitize a raw string into a clean alphanumeric Telegram start tag."""
    import re
    # Transliterate Russian characters if present
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    s = raw.lower().strip()
    s = s.lstrip("@").strip()
    res = []
    for ch in s:
        if ch in translit_map:
            res.append(translit_map[ch])
        elif ch.isalnum() or ch in ('_', '-'):
            res.append(ch)
        elif ch in (' ', '.', '/', ':'):
            res.append('_')
    slug = "".join(res)
    slug = re.sub(r'[_]{2,}', '_', slug).strip('_')
    if not slug or slug.isdigit() or slug.startswith("item_"):
        slug = f"ad_{slug or 'link'}"
    return slug[:60]


async def create_traffic_campaign(
    name: str,
    tag: str | None = None,
    created_by: int | None = None,
    cost: Decimal = Decimal(0),
) -> dict:
    """Create a new traffic tracking link / ad procurement campaign."""
    base_tag = _clean_slug(tag or name)
    unique_tag = base_tag

    async with Database().session() as s:
        # Ensure unique tag
        counter = 1
        while True:
            existing = (await s.execute(
                select(TrafficCampaign.id).where(TrafficCampaign.tag == unique_tag)
            )).scalar()
            if not existing:
                break
            counter += 1
            unique_tag = f"{base_tag}_{counter}"

        campaign = TrafficCampaign(
            name=name.strip()[:128],
            tag=unique_tag,
            cost=cost,
            created_by=created_by,
        )
        s.add(campaign)
        await s.flush()
        return {
            "id": campaign.id,
            "name": campaign.name,
            "tag": campaign.tag,
            "cost": campaign.cost,
            "created_at": campaign.created_at,
            "created_by": campaign.created_by,
        }


async def get_traffic_campaign_by_tag(tag: str) -> dict | None:
    """Find active campaign by its start tag."""
    if not tag:
        return None
    async with Database().session() as s:
        res = (await s.execute(
            select(TrafficCampaign).where(TrafficCampaign.tag == tag.strip().lower())
        )).scalar_one_or_none()
        if not res:
            return None
        return {
            "id": res.id,
            "name": res.name,
            "tag": res.tag,
            "cost": res.cost,
            "created_at": res.created_at,
            "created_by": res.created_by,
        }


async def get_traffic_campaign_by_id(campaign_id: int) -> dict | None:
    """Find campaign by its primary key ID."""
    async with Database().session() as s:
        res = (await s.execute(
            select(TrafficCampaign).where(TrafficCampaign.id == campaign_id)
        )).scalar_one_or_none()
        if not res:
            return None
        return {
            "id": res.id,
            "name": res.name,
            "tag": res.tag,
            "cost": res.cost,
            "created_at": res.created_at,
            "created_by": res.created_by,
        }


async def list_traffic_campaigns() -> list[dict]:
    """List all traffic campaigns with summary metrics."""
    async with Database().session() as s:
        campaigns = (await s.execute(
            select(TrafficCampaign).order_by(desc(TrafficCampaign.id))
        )).scalars().all()

        result = []
        for c in campaigns:
            # Count unique clicks and unique new users
            clicks_count = (await s.execute(
                select(func.count(func.distinct(TrafficClick.user_id))).where(TrafficClick.campaign_id == c.id)
            )).scalar() or 0

            new_users_count = (await s.execute(
                select(func.count(func.distinct(TrafficClick.user_id)))
                .where(TrafficClick.campaign_id == c.id, TrafficClick.is_new_user.is_(True))
            )).scalar() or 0

            # Count total buyers & revenue from users attributed to this campaign
            revenue_stats = (await s.execute(
                select(
                    func.count(BoughtGoods.id),
                    func.coalesce(func.sum(BoughtGoods.price), 0),
                )
                .join(User, User.telegram_id == BoughtGoods.buyer_id)
                .where(User.source_campaign_id == c.id)
            )).first()

            purchases_count = revenue_stats[0] if revenue_stats else 0
            revenue_total = Decimal(str(revenue_stats[1])) if revenue_stats else Decimal(0)

            result.append({
                "id": c.id,
                "name": c.name,
                "tag": c.tag,
                "cost": c.cost,
                "created_at": c.created_at,
                "created_by": c.created_by,
                "clicks": clicks_count,
                "new_users": new_users_count,
                "purchases": purchases_count,
                "revenue": revenue_total,
            })
        return result


async def delete_traffic_campaign(campaign_id: int) -> bool:
    """Delete a campaign and cascade its click records."""
    async with Database().session() as s:
        res = await s.execute(
            delete(TrafficCampaign).where(TrafficCampaign.id == campaign_id)
        )
        return (res.rowcount or 0) > 0


async def record_traffic_click(campaign_id: int, user_id: int, is_new_user: bool = True) -> bool:
    """Record an ad click / entry event only once per unique user.

    Existing users who previously started the bot or already clicked this
    campaign are excluded from click and new user metrics.
    """
    async with Database().session() as s:
        # 1. Do not count users who already have an account in the bot prior to this
        if not is_new_user:
            return False

        # 2. Prevent duplicate clicks by the same user ID in this campaign
        existing = (await s.execute(
            select(TrafficClick.id).where(
                TrafficClick.campaign_id == campaign_id,
                TrafficClick.user_id == user_id,
            )
        )).scalar()
        if existing:
            return False

        click = TrafficClick(
            campaign_id=campaign_id,
            user_id=user_id,
            is_new_user=True,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        s.add(click)
        return True


async def get_campaign_hourly_stats(campaign_id: int, hours: int = 24) -> dict:
    """Aggregate unique clicks by hour for the past N hours (default 24h)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=hours)

    async with Database().session() as s:
        clicks = (await s.execute(
            select(TrafficClick)
            .where(
                TrafficClick.campaign_id == campaign_id,
                TrafficClick.created_at >= since,
            )
            .order_by(TrafficClick.created_at.asc())
        )).scalars().all()

        # Build hour slots
        slots = {}
        for i in range(hours):
            slot_time = (since + datetime.timedelta(hours=i + 1)).astimezone(MOSCOW_TZ)
            hour_key = slot_time.strftime("%H:00")
            slots[hour_key] = {"hour": hour_key, "clicks": 0, "new_users": 0}

        total_clicks = 0
        total_new_users = 0
        seen_users = set()
        for c in clicks:
            if c.user_id in seen_users:
                continue
            seen_users.add(c.user_id)
            msk_time = c.created_at.astimezone(MOSCOW_TZ)
            hour_key = msk_time.strftime("%H:00")
            if hour_key in slots:
                slots[hour_key]["clicks"] += 1
                if c.is_new_user:
                    slots[hour_key]["new_users"] += 1
            total_clicks += 1
            if c.is_new_user:
                total_new_users += 1

        hourly_list = list(slots.values())
        peak_slot = max(hourly_list, key=lambda x: x["clicks"]) if hourly_list else None

        return {
            "hourly_data": hourly_list,
            "total_clicks": total_clicks,
            "new_users": total_new_users,
            "peak_hour": peak_slot["hour"] if (peak_slot and peak_slot["clicks"] > 0) else "—",
            "peak_clicks": peak_slot["clicks"] if peak_slot else 0,
        }


def render_hourly_chart(hourly_data: list[dict], max_bar_width: int = 12) -> str:
    """Render an ASCII/Unicode bar chart representing hourly traffic distribution."""
    if not hourly_data:
        return "<i>Нет данных о переходах за этот период.</i>"

    max_val = max((item["clicks"] for item in hourly_data), default=0)
    lines = ["<code>"]
    for item in hourly_data:
        hour = item["hour"]
        count = item["clicks"]
        new_u = item.get("new_users", 0)

        if max_val > 0 and count > 0:
            # Scale bar length up to max_bar_width
            bar_len = max(1, int(round((count / max_val) * max_bar_width)))
            bar = "█" * bar_len
            flag = " 🔥" if count == max_val and max_val >= 3 else ""
            line = f"{hour} │ {bar} {count} (+{new_u}){flag}"
        else:
            line = f"{hour} │ · 0"
        lines.append(line)
    lines.append("</code>")
    return "\n".join(lines)
