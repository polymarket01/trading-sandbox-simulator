from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

BOT_UID_PREFIX = "9"
USER_UID_PREFIX = "1"
MIN_UID_DIGITS = 4


def uid_prefix_for_role(role: str) -> str:
    return BOT_UID_PREFIX if role == "mm_bot" else USER_UID_PREFIX


def uid_matches_rule(user_id: int, role: str) -> bool:
    text = str(user_id)
    return len(text) >= MIN_UID_DIGITS and text.startswith(uid_prefix_for_role(role))


def uid_rule_status(user_id: int, role: str) -> tuple[bool, str]:
    prefix = uid_prefix_for_role(role)
    ok = uid_matches_rule(user_id, role)
    return ok, f"{role} uid 应至少 {MIN_UID_DIGITS} 位并以 {prefix} 开头"


def next_prefixed_uid(existing_ids: list[int], role: str) -> int:
    prefix = uid_prefix_for_role(role)
    start = int(prefix + ("0" * (MIN_UID_DIGITS - 1)))
    prefixed = [
        user_id
        for user_id in existing_ids
        if len(str(user_id)) >= MIN_UID_DIGITS and str(user_id).startswith(prefix)
    ]
    candidate = (max(prefixed) + 1) if prefixed else start
    while not uid_matches_rule(candidate, role):
        width = max(MIN_UID_DIGITS, len(str(candidate)))
        candidate = int(prefix + ("0" * (width - 1)))
    return candidate


async def next_uid_for_role(session: AsyncSession, role: str) -> int:
    rows = await session.execute(select(User.id))
    return next_prefixed_uid([int(item[0]) for item in rows.all()], role)
