"""Add database invariants for money, catalog values and purchase quantities.

The preflight deliberately aborts when legacy rows violate an invariant.  It
is safer to stop deployment and repair an ambiguous catalog/payment row than to
silently rewrite prices or payment amounts.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHECKS = {
    "roles": [
        ("ck_roles_permissions_known_bits", "permissions IS NULL OR (permissions >= 0 AND permissions <= 1023)"),
    ],
    "goods": [
        ("ck_goods_price_positive", "CAST(price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND price > 0"),
        ("ck_goods_sale_percent_range", "sale_percent IS NULL OR (CAST(sale_percent AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND sale_percent >= 0 AND sale_percent <= 100)"),
    ],
    "item_values": [
        ("ck_item_values_value_nonempty", "value IS NOT NULL AND length(trim(value)) > 0"),
    ],
    "payments": [
        ("ck_payments_amount_positive", "CAST(amount AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND amount > 0"),
    ],
    "cart_items": [
        ("ck_cart_items_quantity_range", "quantity > 0 AND quantity <= 99"),
    ],
}


_PREFLIGHTS = {
    "roles": "permissions IS NOT NULL AND (permissions < 0 OR permissions > 1023)",
    "goods": (
        "CAST(price AS TEXT) IN ('NaN', 'Infinity', '-Infinity') OR price <= 0 "
        "OR (sale_percent IS NOT NULL AND ("
        "CAST(sale_percent AS TEXT) IN ('NaN', 'Infinity', '-Infinity') "
        "OR sale_percent < 0 OR sale_percent > 100 "
        "OR (sale_percent = 100 AND sale_until IS NOT NULL AND sale_until > CURRENT_TIMESTAMP)"
        "))"
    ),
    "item_values": "value IS NULL OR length(trim(value)) = 0",
    "payments": "CAST(amount AS TEXT) IN ('NaN', 'Infinity', '-Infinity') OR amount <= 0",
    "cart_items": "quantity <= 0 OR quantity > 99",
}


def upgrade() -> None:
    bind = op.get_bind()
    for table, predicate in _PREFLIGHTS.items():
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}")
        ).scalar_one()
        if count:
            raise RuntimeError(
                f"Cannot add {table} invariants: {count} legacy row(s) violate {predicate}. "
                "Repair the rows and rerun the migration."
            )

    inspector = sa.inspect(bind)
    for table, constraints in _CHECKS.items():
        existing = {
            item.get("name") for item in inspector.get_check_constraints(table)
        }
        for name, expression in constraints:
            if name not in existing:
                op.create_check_constraint(name, table, expression)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, constraints in reversed(list(_CHECKS.items())):
        existing = {
            item.get("name") for item in inspector.get_check_constraints(table)
        }
        for name, _expression in reversed(constraints):
            if name in existing:
                op.drop_constraint(name, table_name=table, type_="check")
