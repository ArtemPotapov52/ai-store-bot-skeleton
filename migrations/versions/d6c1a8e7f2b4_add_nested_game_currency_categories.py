"""add nested game currency categories"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d6c1a8e7f2b4"
down_revision: Union[str, None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROOT_NAME = "Игровая валюта"
CHILD_NAMES = (
    "Гемы БС",
    "Голда Standoff 2",
    "Brawl Pass",
    "Звезды",
    "Робуксы",
    "Пополнение Steam",
)


def upgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_categories_parent_id_categories",
            "categories",
            ["parent_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_categories_parent_id", ["parent_id"])

    bind = op.get_bind()
    categories = sa.table(
        "categories",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String(100)),
        sa.column("parent_id", sa.Integer()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    goods = sa.table(
        "goods",
        sa.column("id", sa.Integer()),
        sa.column("category_id", sa.Integer()),
    )

    root_id = bind.execute(
        sa.select(categories.c.id).where(categories.c.name == ROOT_NAME)
    ).scalar_one_or_none()
    if root_id is not None:
        root_product_count = bind.execute(
            sa.select(sa.func.count(goods.c.id)).where(goods.c.category_id == root_id)
        ).scalar_one()
        if root_product_count:
            raise RuntimeError(
                "Cannot convert 'Игровая валюта' into a navigation group while it "
                "contains products; move those products to a leaf category first."
            )
        max_order = bind.execute(
            sa.select(sa.func.max(categories.c.sort_order)).where(
                categories.c.id != root_id,
                categories.c.parent_id.is_(None),
            )
        ).scalar_one()
        root_order = min(int(max_order or 0) + 1, 2_147_483_647)
        bind.execute(
            sa.update(categories).where(categories.c.id == root_id).values(
                parent_id=None, sort_order=root_order, is_active=True
            )
        )
    else:
        max_order = bind.execute(
            sa.select(sa.func.max(categories.c.sort_order)).where(
                categories.c.parent_id.is_(None)
            )
        ).scalar_one()
        root_order = min(int(max_order or 0) + 1, 2_147_483_647)
        root_id = bind.execute(
            sa.insert(categories).values(
                name=ROOT_NAME,
                parent_id=None,
                sort_order=root_order,
                is_active=True,
            ).returning(categories.c.id)
        ).scalar_one()

    for sort_order, name in enumerate(CHILD_NAMES):
        child_id = bind.execute(
            sa.select(categories.c.id).where(categories.c.name == name)
        ).scalar_one_or_none()
        if child_id is None:
            bind.execute(
                sa.insert(categories).values(
                    name=name,
                    parent_id=root_id,
                    sort_order=sort_order,
                    is_active=True,
                )
            )
            continue

        bind.execute(
            sa.update(categories).where(categories.c.id == child_id).values(
                parent_id=root_id,
                sort_order=sort_order,
                is_active=True,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    categories = sa.table(
        "categories",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String(100)),
        sa.column("parent_id", sa.Integer()),
    )
    # Keep categories and any products assigned to them if the schema change is
    # rolled back; flatten the tree instead of deleting merchant data.
    bind.execute(
        sa.update(categories)
        .where(categories.c.name.in_((*CHILD_NAMES, ROOT_NAME)))
        .values(parent_id=None)
    )
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_index("ix_categories_parent_id")
        batch_op.drop_constraint("fk_categories_parent_id_categories", type_="foreignkey")
        batch_op.drop_column("parent_id")
