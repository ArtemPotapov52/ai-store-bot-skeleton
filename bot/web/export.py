import csv
import io
from datetime import datetime

from starlette.requests import Request
from starlette.responses import StreamingResponse, JSONResponse
from starlette.routing import Route
from sqlalchemy import select

from bot.database.main import Database
from bot.database.models.main import Permission, User, BoughtGoods, Operations, Payments
from bot.database.methods.audit import log_audit
from bot.web.access import client_ip_from_scope, has_web_perm, web_session_active
from bot.misc.timezone import moscow_day_window, moscow_isoformat


BATCH_SIZE = 1000

# Leading characters that spreadsheet apps interpret as the start of a formula.
_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value) -> str:
    """Neutralize CSV/Excel formula injection by quoting risky leading chars."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in _INJECTION_PREFIXES:
        return "'" + s
    return s


async def _stream_csv(query, columns, session_maker, keyset_column):
    """Generic CSV streamer using keyset pagination on ``keyset_column``.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow(columns)
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    last_key = None
    while True:
        batch_query = query
        if last_key is not None:
            batch_query = batch_query.where(keyset_column > last_key)
        batch_query = batch_query.limit(BATCH_SIZE)

        async with session_maker() as s:
            result = await s.execute(batch_query)
            rows = result.all()

        if not rows:
            break

        for row in rows:
            values = [getattr(row, c, row[i]) if hasattr(row, c) else row[i] for i, c in enumerate(columns)]
            writer.writerow([
                _sanitize_cell(moscow_isoformat(value) if isinstance(value, datetime) else value)
                for value in values
            ])

        last_key = rows[-1][0]
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        if len(rows) < BATCH_SIZE:
            break


def _parse_date_params(request: Request):
    """Parse from/to date query params."""
    from_str = request.query_params.get("from")
    to_str = request.query_params.get("to")
    from_date = None
    to_date = None
    if from_str:
        try:
            from_date, _ = moscow_day_window(from_str)
        except ValueError:
            pass
    if to_str:
        try:
            to_date, _ = moscow_day_window(to_str)
        except ValueError:
            pass
    return from_date, to_date


async def _require(request: Request, bit: int, name: str):
    """401 when logged out, 403 without the permission; audits downloads."""
    if not await web_session_active(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not await has_web_perm(request, bit):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    await log_audit(
        name,
        details=f"user={request.session.get('web_login', '?')}",
        ip_address=client_ip_from_scope(request.scope, dict(request.headers)),
    )
    return None


async def export_users(request: Request):
    denied = await _require(request, Permission.USERS_MANAGE, "export_users")
    if denied is not None:
        return denied

    from_date, to_date = _parse_date_params(request)
    query = select(
        User.telegram_id, User.balance, User.role_id,
        User.referral_id, User.registration_date, User.is_blocked
    ).order_by(User.telegram_id)

    if from_date:
        query = query.where(User.registration_date >= from_date)
    if to_date:
        query = query.where(User.registration_date < to_date)

    columns = ["telegram_id", "balance", "role_id", "referral_id", "registration_date", "is_blocked"]

    return StreamingResponse(
        _stream_csv(query, columns, Database().session, User.telegram_id),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


async def export_purchases(request: Request):
    denied = await _require(request, Permission.STATS_VIEW, "export_purchases")
    if denied is not None:
        return denied

    from_date, to_date = _parse_date_params(request)
    query = select(
        BoughtGoods.id, BoughtGoods.item_name, BoughtGoods.price,
        BoughtGoods.buyer_id, BoughtGoods.bought_datetime, BoughtGoods.unique_id
    ).order_by(BoughtGoods.id)

    if from_date:
        query = query.where(BoughtGoods.bought_datetime >= from_date)
    if to_date:
        query = query.where(BoughtGoods.bought_datetime < to_date)

    columns = ["id", "item_name", "price", "buyer_id", "bought_datetime", "unique_id"]

    return StreamingResponse(
        _stream_csv(query, columns, Database().session, BoughtGoods.id),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=purchases.csv"},
    )


async def export_operations(request: Request):
    denied = await _require(request, Permission.STATS_VIEW, "export_operations")
    if denied is not None:
        return denied

    from_date, to_date = _parse_date_params(request)
    query = select(
        Operations.id, Operations.user_id, Operations.operation_value,
        Operations.operation_time
    ).order_by(Operations.id)

    if from_date:
        query = query.where(Operations.operation_time >= from_date)
    if to_date:
        query = query.where(Operations.operation_time < to_date)

    columns = ["id", "user_id", "operation_value", "operation_time"]

    return StreamingResponse(
        _stream_csv(query, columns, Database().session, Operations.id),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=operations.csv"},
    )


async def export_payments(request: Request):
    denied = await _require(request, Permission.STATS_VIEW, "export_payments")
    if denied is not None:
        return denied

    from_date, to_date = _parse_date_params(request)
    query = select(
        Payments.id, Payments.provider, Payments.external_id,
        Payments.user_id, Payments.amount, Payments.currency,
        Payments.status, Payments.created_at
    ).order_by(Payments.id)

    if from_date:
        query = query.where(Payments.created_at >= from_date)
    if to_date:
        query = query.where(Payments.created_at < to_date)

    columns = ["id", "provider", "external_id", "user_id", "amount", "currency", "status", "created_at"]

    return StreamingResponse(
        _stream_csv(query, columns, Database().session, Payments.id),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payments.csv"},
    )


export_routes = [
    Route("/export/users", export_users),
    Route("/export/purchases", export_purchases),
    Route("/export/operations", export_operations),
    Route("/export/payments", export_payments),
]
