"""Durable per-API-key idempotency records for mutating requests."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.database import Database
from bot.database.models import ApiIdempotency
from bot.web.api.common import ApiError


def request_digest(operation: str, body: dict) -> str:
    canonical = json.dumps(
        {"operation": operation, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def begin_idempotency(request, operation: str, body: dict):
    """Return (row id, saved result) or raise a stable replay/conflict error."""
    key = request.headers.get("idempotency-key", "")
    if (
        not key
        or len(key) > 128
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise ApiError(400, "idempotency_key_required", "Provide an Idempotency-Key of 1–128 printable ASCII characters.")

    api_key_id = int(request.state.api_key_id)
    digest = request_digest(operation, body)

    try:
        async with Database().session() as session:
            row = (await session.execute(
                select(ApiIdempotency).where(
                    ApiIdempotency.api_key_id == api_key_id,
                    ApiIdempotency.idempotency_key == key,
                ).with_for_update()
            )).scalar_one_or_none()
            if row is not None:
                return _existing(row, operation, digest)

            row = ApiIdempotency(
                api_key_id=api_key_id,
                idempotency_key=key,
                operation=operation,
                request_hash=digest,
                status="processing",
            )
            session.add(row)
            await session.flush()
            row_id = int(row.id)
        return row_id, None
    except IntegrityError:
        # A concurrent request won the unique-key insert. The session context
        # rolls back, then a fresh read below returns that request's state.
        pass

    async with Database().session() as session:
        row = (await session.execute(
            select(ApiIdempotency).where(
                ApiIdempotency.api_key_id == api_key_id,
                ApiIdempotency.idempotency_key == key,
            )
        )).scalar_one_or_none()
        if row is None:
            raise ApiError(503, "idempotency_unavailable", "Could not safely establish the request state; retry with the same key.")
        return _existing(row, operation, digest)


def _existing(row: ApiIdempotency, operation: str, digest: str):
    if row.operation != operation or row.request_hash != digest:
        raise ApiError(409, "idempotency_key_reused", "This Idempotency-Key was already used for a different request.")
    if row.status == "completed":
        return int(row.id), row.result_json or {}
    if row.status == "failed":
        result = row.result_json or {}
        raise ApiError(
            int(result.get("status_code", 409)),
            str(result.get("code", "request_failed")),
            str(result.get("message", "The original request failed.")),
        )
    raise ApiError(409, "request_in_progress", "A request with this key is still processing; retry the same key shortly.")


async def finish_idempotency(row_id: int, *, status: str, result: dict) -> None:
    """Persist a non-purchase result, or a sanitized terminal purchase error."""
    if status not in {"completed", "failed"}:
        raise ValueError("Invalid idempotency terminal state")
    async with Database().session() as session:
        row = (await session.execute(
            select(ApiIdempotency).where(ApiIdempotency.id == int(row_id)).with_for_update()
        )).scalar_one_or_none()
        if row is not None and row.status == "processing":
            row.status = status
            row.result_json = result
