"""Allow variable-priced product orders up to 5,000 units."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.drop_constraint("ck_goods_variable_quantity_range", "goods", type_="check")
    op.create_check_constraint(
        "ck_goods_variable_quantity_range",
        "goods",
        "(is_variable_pricing = false AND min_quantity IS NULL AND max_quantity IS NULL) "
        "OR (is_variable_pricing = true AND min_quantity >= 1 "
        "AND max_quantity >= min_quantity AND max_quantity <= 5000)",
    )
    op.drop_constraint("ck_cart_items_quantity_range", "cart_items", type_="check")
    op.create_check_constraint(
        "ck_cart_items_quantity_range", "cart_items", "quantity > 0 AND quantity <= 5000"
    )


def downgrade() -> None:
    bind = op.get_bind()
    oversized_goods = bind.execute(sa.text(
        "SELECT count(*) FROM goods WHERE is_variable_pricing = true AND max_quantity > 99"
    )).scalar_one()
    oversized_cart = bind.execute(sa.text(
        "SELECT count(*) FROM cart_items WHERE quantity > 99"
    )).scalar_one()
    if oversized_goods or oversized_cart:
        raise RuntimeError(
            "Cannot restore the 99-unit limits while variable products or carts "
            "contain a larger quantity."
        )

    op.drop_constraint("ck_cart_items_quantity_range", "cart_items", type_="check")
    op.create_check_constraint(
        "ck_cart_items_quantity_range", "cart_items", "quantity > 0 AND quantity <= 99"
    )
    op.drop_constraint("ck_goods_variable_quantity_range", "goods", type_="check")
    op.create_check_constraint(
        "ck_goods_variable_quantity_range",
        "goods",
        "(is_variable_pricing = false AND min_quantity IS NULL AND max_quantity IS NULL) "
        "OR (is_variable_pricing = true AND min_quantity >= 1 "
        "AND max_quantity >= min_quantity AND max_quantity <= 99)",
    )
