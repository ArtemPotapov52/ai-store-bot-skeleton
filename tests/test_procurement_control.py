from datetime import date
from decimal import Decimal
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _Form:
    def __init__(self, **values):
        self.values = values

    def getlist(self, name):
        value = self.values.get(name, [])
        return value if isinstance(value, list) else [value]

    def get(self, name, default=None):
        value = self.values.get(name, default)
        return value[-1] if isinstance(value, list) and value else value


def _form(**overrides):
    values = {
        "category_id": ["2", "2"],
        "product_id": ["10", "11"],
        "quantity": ["2", "3"],
        "unit_cost": ["30.50", "10"],
        "sale_mode": ["catalog", "manual"],
        "custom_sale_price": ["", "20"],
    }
    values.update(overrides)
    return _Form(**values)


class TestProcurementInput:
    def test_parses_catalog_and_custom_prices_for_multiple_products(self):
        from bot.web.procurement import parse_procurement_form

        lines = parse_procurement_form(
            _form(), {10: Decimal("75.00"), 11: Decimal("99.00")}
        )

        assert [(line.product_id, line.quantity, line.unit_cost, line.sale_price) for line in lines] == [
            (10, 2, Decimal("30.50"), Decimal("75.00")),
            (11, 3, Decimal("10.00"), Decimal("20.00")),
        ]
        assert [line.sale_price_mode for line in lines] == ["catalog", "manual"]

    @pytest.mark.parametrize("overrides", [
        {"category_id": []},
        {"quantity": ["1"]},
        {"quantity": ["1", "100001"]},
        {"unit_cost": ["NaN", "10"]},
        {"unit_cost": ["0", "10"]},
        {"unit_cost": ["10000000000", "10"]},
        {"sale_mode": ["other", "manual"]},
        {"sale_mode": ["catalog", "manual"], "custom_sale_price": ["", "Infinity"]},
        {"product_id": ["10", "10"]},
    ])
    def test_rejects_invalid_or_duplicate_lines(self, overrides):
        from bot.web.procurement import ProcurementInputError, parse_procurement_form

        with pytest.raises(ProcurementInputError):
            parse_procurement_form(
                _form(**overrides), {10: Decimal("75"), 11: Decimal("99")}
            )

    def test_requires_server_catalog_price_and_limits_basket_size(self):
        from bot.web.procurement import ProcurementInputError, parse_procurement_form

        with pytest.raises(ProcurementInputError, match="цен"):
            parse_procurement_form(_form(), {11: Decimal("99")})

        fifty_one = {
            key: value * 26 for key, value in _form().values.items()
        }
        with pytest.raises(ProcurementInputError):
            parse_procurement_form(_Form(**fifty_one), {10: Decimal("75"), 11: Decimal("99")})


class TestProcurementForecast:
    def test_calculates_cost_revenue_profit_margin_and_return_on_cost(self):
        from bot.web.procurement import calculate_forecast, parse_procurement_form

        lines = parse_procurement_form(
            _form(), {10: Decimal("75.00"), 11: Decimal("99.00")}
        )
        result = calculate_forecast(lines)

        assert result == {
            "total_cost": Decimal("91.00"),
            "expected_revenue": Decimal("210.00"),
            "gross_profit": Decimal("119.00"),
            "gross_margin_percent": Decimal("56.67"),
            "return_on_cost_percent": Decimal("130.77"),
            "quantity": 5,
            "line_count": 2,
        }

    def test_negative_profit_is_supported_and_zero_division_is_safe(self):
        from bot.web.procurement import calculate_forecast

        result = calculate_forecast([
            SimpleNamespace(quantity=1, unit_cost=Decimal("100"), sale_price=Decimal("1"))
        ])

        assert result["gross_profit"] == Decimal("-99.00")
        assert result["gross_margin_percent"] == Decimal("-9900.00")
        assert result["return_on_cost_percent"] == Decimal("-99.00")

    def test_groups_only_saved_forecast_dates(self):
        from bot.web.procurement import build_daily_procurement_report

        plans = [
            SimpleNamespace(
                plan_date=date(2026, 9, 24), total_cost=Decimal("50"),
                expected_revenue=Decimal("100"), gross_profit=Decimal("50"), item_count=2,
            ),
            SimpleNamespace(
                plan_date=date(2026, 9, 24), total_cost=Decimal("30"),
                expected_revenue=Decimal("40"), gross_profit=Decimal("10"), item_count=1,
            ),
            SimpleNamespace(
                plan_date=date(2026, 9, 25), total_cost=Decimal("10"),
                expected_revenue=Decimal("5"), gross_profit=Decimal("-5"), item_count=1,
            ),
        ]

        report = build_daily_procurement_report(plans)

        assert [row["date"] for row in report] == [date(2026, 9, 24), date(2026, 9, 25)]
        assert report[0] == {
            "date": date(2026, 9, 24),
            "plans": 2,
            "items": 3,
            "total_cost": Decimal("80.00"),
            "expected_revenue": Decimal("140.00"),
            "gross_profit": Decimal("60.00"),
        }

    def test_plan_metadata_validates_moscow_calendar_date_and_title(self):
        from bot.web.procurement import ProcurementInputError, parse_plan_metadata

        assert parse_plan_metadata(_Form(plan_date="2026-09-25", title="Закупка")) == (
            date(2026, 9, 25), "Закупка"
        )
        assert parse_plan_metadata(_Form(plan_date="2026-09-25", title="  ")) == (
            date(2026, 9, 25), None
        )
        with pytest.raises(ProcurementInputError):
            parse_plan_metadata(_Form(plan_date="25.09.2026", title=""))


class TestProcurementMigration:
    def test_migration_preserves_referenced_tables_and_downgrades_cleanly(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect, text

        migration_path = (
            Path(__file__).parents[1]
            / "migrations"
            / "versions"
            / "e7f8a9b0c1d2_add_procurement_plans.py"
        )
        spec = importlib.util.spec_from_file_location("procurement_migration", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE goods (id INTEGER PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE categories (id INTEGER PRIMARY KEY)"))
            migration_context = MigrationContext.configure(connection)
            with Operations.context(migration_context):
                migration.upgrade()

            inspector = inspect(connection)
            assert {"goods", "categories", "procurement_plans", "procurement_plan_items"}.issubset(
                set(inspector.get_table_names())
            )
            item_columns = {column["name"] for column in inspector.get_columns("procurement_plan_items")}
            assert {
                "id", "plan_id", "product_id", "category_id", "category_name", "product_name",
                "quantity", "unit_cost", "sale_price", "sale_price_mode", "total_cost",
                "expected_revenue", "gross_profit",
            } == item_columns
            fks = {fk["referred_table"]: fk["options"].get("ondelete")
                   for fk in inspector.get_foreign_keys("procurement_plan_items")}
            assert fks == {"procurement_plans": "CASCADE", "goods": "SET NULL", "categories": "SET NULL"}

            with Operations.context(migration_context):
                migration.downgrade()
            assert "procurement_plans" not in inspect(connection).get_table_names()
            assert "procurement_plan_items" not in inspect(connection).get_table_names()
            assert {"goods", "categories"}.issubset(set(inspect(connection).get_table_names()))


class _TemplateStub:
    async def TemplateResponse(self, _request, _template, context):
        return context


def _request(form=None, *, method="GET", query_params=None):
    return SimpleNamespace(
        method=method,
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
        form=AsyncMock(return_value=form or _Form()),
        query_params=query_params or {},
        session={"web_login": "procurement-tester"},
    )


class TestProcurementControlView:
    def test_daily_history_query_has_postgres_compatible_aggregates(self):
        from sqlalchemy.dialects import postgresql

        from bot.web.admin import _procurement_daily_query

        statement = _procurement_daily_query()
        sql = str(statement.compile(dialect=postgresql.dialect()))

        assert sql.count("GROUP BY procurement_plans.plan_date") == 2
        assert "count(procurement_plans.id) AS plan_count" in sql
        assert "count(procurement_plan_items.id) AS item_count" in sql
        assert "LEFT OUTER JOIN" in sql

    def test_template_compiles_and_view_is_registered(self):
        from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
        from sqladmin import BaseView, ModelView
        from starlette.routing import Mount

        import bot.web.admin as web_admin

        templates_path = Path(__file__).parents[1] / "bot" / "web" / "templates"
        template = Environment(loader=FileSystemLoader(templates_path)).get_template(
            "procurement.html"
        )
        assert template.name == "procurement.html"
        environment = Environment(loader=ChoiceLoader([
            DictLoader({
                "sqladmin/layout.html": "{% block head %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(templates_path),
        ]), autoescape=True)
        rendered = environment.get_template("procurement.html").render(
            categories=[{"id": 3, "name": "Категория"}],
            products=[{
                "id": 15, "category_id": 3, "category_name": "Категория",
                "name": "Тестовый товар", "current_price": Decimal("123.45"),
            }],
            daily=[], history=[], history_page=1, history_pages=1, history_total=0,
            draft_rows=[{
                "category_id": "", "product_id": "", "quantity": "",
                "unit_cost": "", "sale_mode": "catalog", "custom_sale_price": "",
            }],
            plan_date="2026-09-25", title="", error=None, success=None,
            can_save=True, currency="RUB",
        )
        assert "Тестовый товар" in rendered
        assert 'aria-live="polite"' in rendered
        assert "Сохранить прогноз" in rendered

        view_classes = [
            value for value in vars(web_admin).values()
            if isinstance(value, type)
            and (issubclass(value, BaseView) or issubclass(value, ModelView))
        ]
        previous_refs = {
            view: (hasattr(view, "_admin_ref"), getattr(view, "_admin_ref", None))
            for view in view_classes
        }
        try:
            app = web_admin.create_admin_app()
            admin_app = next(
                route.app for route in app.routes
                if isinstance(route, Mount) and route.path == "/admin"
            )
            assert any(route.path == "/procurement-control" for route in admin_app.routes)
            assert web_admin.ProcurementControlView.required_perm == web_admin.Permission.STATS_VIEW
            rbac = next(
                middleware for middleware in app.user_middleware
                if middleware.cls.__name__ == "WebRBACMiddleware"
            )
            assert rbac.kwargs["perm_map"]["procurement-control"] == web_admin.Permission.STATS_VIEW
        finally:
            for view, (had_admin_ref, previous_admin_ref) in previous_refs.items():
                if had_admin_ref:
                    view._admin_ref = previous_admin_ref
                elif hasattr(view, "_admin_ref"):
                    delattr(view, "_admin_ref")

    async def test_saving_plan_snapshots_catalog_and_does_not_change_real_stock_or_expenses(
        self, item_factory,
    ):
        from sqlalchemy import func, select
        from starlette.responses import RedirectResponse

        from bot.database.main import Database
        from bot.database.models.main import Goods, ProductExpense, ProcurementPlan, ProcurementPlanItem
        from bot.database.models.main import Permission
        from bot.web.admin import ProcurementControlView

        await item_factory(name="Forecast product", price=100, stock_quantity=7)
        async with Database().session() as session:
            product = (await session.execute(
                select(Goods).where(Goods.name == "Forecast product")
            )).scalar_one()
            product_id, category_id = int(product.id), int(product.category_id)
            original_price, original_stock = product.price, product.stock_quantity

        request = _request(_Form(
            category_id=[str(category_id)],
            product_id=[str(product_id)],
            quantity=["2"],
            unit_cost=["30.00"],
            sale_mode=["catalog"],
            custom_sale_price=[""],
            plan_date="2026-09-25",
            title="Партия на пятницу",
        ), method="POST")
        audit = AsyncMock()
        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW | Permission.CATALOG_MANAGE
                )), \
                patch("bot.web.admin.log_audit", new=audit):
            response = await ProcurementControlView().procurement_control(request)

        assert isinstance(response, RedirectResponse)
        assert response.status_code == 303
        audit.assert_awaited_once()
        assert audit.await_args.args[0] == "procurement_plan_saved"
        assert audit.await_args.kwargs["session"] is not None

        async with Database().session() as session:
            plan = (await session.execute(select(ProcurementPlan))).scalar_one()
            item = (await session.execute(select(ProcurementPlanItem))).scalar_one()
            product = (await session.execute(
                select(Goods).where(Goods.id == product_id)
            )).scalar_one()
            expense_count = await session.scalar(select(func.count(ProductExpense.id)))

        assert plan.plan_date == date(2026, 9, 25)
        assert plan.title == "Партия на пятницу"
        assert plan.total_cost == Decimal("60.00")
        assert plan.expected_revenue == Decimal("200.00")
        assert plan.gross_profit == Decimal("140.00")
        assert item.product_name == "Forecast product"
        assert item.category_name == "TestCategory"
        assert item.sale_price_mode == "catalog"
        assert item.sale_price == Decimal("100.00")
        assert product.price == original_price
        assert product.stock_quantity == original_stock == 7
        assert expense_count == 0

        async with Database().session() as session:
            product = (await session.execute(
                select(Goods).where(Goods.id == product_id)
            )).scalar_one()
            product.name = "Renamed later"
            product.price = Decimal("150.00")
        async with Database().session() as session:
            saved_item = (await session.execute(select(ProcurementPlanItem))).scalar_one()
        assert saved_item.product_name == "Forecast product"
        assert saved_item.sale_price == Decimal("100.00")

    async def test_missing_catalog_permission_cannot_save_a_plan(self):
        from sqlalchemy import func, select

        from bot.database.main import Database
        from bot.database.models.main import Permission, ProcurementPlan
        from bot.web.admin import ProcurementControlView

        request = _request(_Form(
            category_id=["1"], product_id=["1"], quantity=["1"],
            unit_cost=["10"], sale_mode=["manual"], custom_sale_price=["20"],
            plan_date="2026-09-25", title="No permission",
        ), method="POST")
        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW
                )):
            context = await ProcurementControlView().procurement_control(request)

        assert "право управления каталогом" in context["error"]
        assert context["can_save"] is False
        async with Database().session() as session:
            assert await session.scalar(select(func.count(ProcurementPlan.id))) == 0

    async def test_rejects_product_from_another_category_without_partial_write(
        self, category_factory, item_factory,
    ):
        from sqlalchemy import func, select

        from bot.database.main import Database
        from bot.database.models.main import Categories, Goods, Permission, ProcurementPlan
        from bot.web.admin import ProcurementControlView

        await category_factory("WrongCategory")
        await item_factory(name="Category bound product", price=90, category="TestCategory")
        async with Database().session() as session:
            wrong_category = (await session.execute(
                select(Categories).where(Categories.name == "WrongCategory")
            )).scalar_one()
            product = (await session.execute(
                select(Goods).where(Goods.name == "Category bound product")
            )).scalar_one()
            wrong_category_id, product_id = int(wrong_category.id), int(product.id)

        request = _request(_Form(
            category_id=[str(wrong_category_id)],
            product_id=[str(product_id)],
            quantity=["2"], unit_cost=["20"], sale_mode=["catalog"],
            custom_sale_price=[""], plan_date="2026-09-25", title="Wrong category",
        ), method="POST")
        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW | Permission.CATALOG_MANAGE
                )), \
                patch("bot.web.admin.log_audit", new=AsyncMock()):
            context = await ProcurementControlView().procurement_control(request)

        assert "не относится" in context["error"]
        async with Database().session() as session:
            assert await session.scalar(select(func.count(ProcurementPlan.id))) == 0

    async def test_manual_sale_price_is_saved_as_the_custom_snapshot(self, item_factory):
        from sqlalchemy import select
        from starlette.responses import RedirectResponse

        from bot.database.main import Database
        from bot.database.models.main import Goods, Permission, ProcurementPlan, ProcurementPlanItem
        from bot.web.admin import ProcurementControlView

        await item_factory(name="Manual sale product", price=100)
        async with Database().session() as session:
            product = (await session.execute(
                select(Goods).where(Goods.name == "Manual sale product")
            )).scalar_one()
            product_id, category_id = int(product.id), int(product.category_id)

        request = _request(_Form(
            category_id=[str(category_id)], product_id=[str(product_id)],
            quantity=["3"], unit_cost=["40"], sale_mode=["manual"],
            custom_sale_price=["55,50"], plan_date="2026-09-25", title="С ручной ценой",
        ), method="POST")
        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW | Permission.CATALOG_MANAGE
                )), \
                patch("bot.web.admin.log_audit", new=AsyncMock()):
            response = await ProcurementControlView().procurement_control(request)

        assert isinstance(response, RedirectResponse)
        async with Database().session() as session:
            plan = (await session.execute(select(ProcurementPlan))).scalar_one()
            item = (await session.execute(select(ProcurementPlanItem))).scalar_one()
            product = (await session.execute(select(Goods).where(Goods.id == product_id))).scalar_one()
        assert item.sale_price_mode == "manual"
        assert item.sale_price == Decimal("55.50")
        assert plan.expected_revenue == Decimal("166.50")
        assert product.price == Decimal("100.00")

    async def test_category_groups_are_hidden_but_active_leaf_products_are_selectable(
        self, category_factory, item_factory,
    ):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Categories, Permission
        from bot.web.admin import ProcurementControlView

        await category_factory("Navigation group")
        async with Database().session() as session:
            parent = (await session.execute(
                select(Categories).where(Categories.name == "Navigation group")
            )).scalar_one()
            child = Categories(name="Selectable leaf", parent_id=parent.id, is_active=True)
            session.add(child)
        await item_factory(name="Leaf product", price=70, category="Selectable leaf")

        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW
                )):
            context = await ProcurementControlView().procurement_control(_request())

        assert {row["name"] for row in context["categories"]} == {"Selectable leaf"}
        assert [row["name"] for row in context["products"]] == ["Leaf product"]

    async def test_get_shows_active_leaf_catalog_and_daily_history(self, item_factory):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Goods, Permission, ProcurementPlan, ProcurementPlanItem
        from bot.web.admin import ProcurementControlView

        await item_factory(name="Visible forecast product", price=42)
        await item_factory(name="Second forecast product", price=12)
        async with Database().session() as session:
            product = (await session.execute(
                select(Goods).where(Goods.name == "Visible forecast product")
            )).scalar_one()
            category_id, product_id = int(product.category_id), int(product.id)
            second_product = (await session.execute(
                select(Goods).where(Goods.name == "Second forecast product")
            )).scalar_one()
            plan = ProcurementPlan(
                plan_date=date(2026, 9, 25), title="Saved history",
                total_cost=Decimal("15.00"), expected_revenue=Decimal("54.00"),
                gross_profit=Decimal("39.00"), gross_margin_percent=Decimal("72.22"),
                return_on_cost_percent=Decimal("260.00"),
                items=[ProcurementPlanItem(
                    product_id=product_id, category_id=category_id,
                    category_name="TestCategory", product_name="Visible forecast product",
                    quantity=1, unit_cost=Decimal("10.00"), sale_price=Decimal("42.00"),
                    sale_price_mode="catalog", total_cost=Decimal("10.00"),
                    expected_revenue=Decimal("42.00"), gross_profit=Decimal("32.00"),
                ), ProcurementPlanItem(
                    product_id=int(second_product.id), category_id=category_id,
                    category_name="TestCategory", product_name="Second forecast product",
                    quantity=1, unit_cost=Decimal("5.00"), sale_price=Decimal("12.00"),
                    sale_price_mode="catalog", total_cost=Decimal("5.00"),
                    expected_revenue=Decimal("12.00"), gross_profit=Decimal("7.00"),
                )],
            )
            session.add(plan)

        request = _request()
        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW
                )):
            context = await ProcurementControlView().procurement_control(request)

        assert context["can_save"] is False
        assert {row["id"] for row in context["categories"]} == {category_id}
        assert {row["id"] for row in context["products"]} == {
            product_id, int(second_product.id),
        }
        assert context["history"][0].title == "Saved history"
        assert context["history"][0].display_items[0].product_name == "Visible forecast product"
        assert context["daily"][0]["plans"] == 1
        assert context["daily"][0]["items"] == 2
        assert context["daily"][0]["total_cost_display"] == "15,00"
        assert context["daily"][0]["expected_revenue_display"] == "54,00"
        assert context["daily"][0]["gross_profit_display"] == "39,00"

    async def test_history_paginates_and_clamps_out_of_range_page(self):
        from bot.database.main import Database
        from bot.database.models.main import Permission, ProcurementPlan
        from bot.web.admin import ProcurementControlView

        async with Database().session() as session:
            session.add_all([
                ProcurementPlan(
                    plan_date=date(2026, 9, 1), title=f"Plan {index}",
                    total_cost=Decimal("1.00"), expected_revenue=Decimal("2.00"),
                    gross_profit=Decimal("1.00"), gross_margin_percent=Decimal("50.00"),
                    return_on_cost_percent=Decimal("100.00"),
                )
                for index in range(51)
            ])

        with patch.object(ProcurementControlView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.resolve_web_perms", new=AsyncMock(
                    return_value=Permission.STATS_VIEW
                )):
            second_page = await ProcurementControlView().procurement_control(
                _request(query_params={"page": "2"})
            )
            clamped_page = await ProcurementControlView().procurement_control(
                _request(query_params={"page": "999"})
            )

        assert len(second_page["history"]) == 1
        assert second_page["history_page"] == 2
        assert second_page["history_pages"] == 2
        assert second_page["history_total"] == 51
        assert len(clamped_page["history"]) == 1
        assert clamped_page["history_page"] == 2
        assert clamped_page["history_pages"] == 2
        assert clamped_page["history_total"] == 51
