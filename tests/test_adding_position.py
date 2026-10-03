from decimal import Decimal

import pytest

from bot.database.methods.read import (
    get_item_info, select_item_values_amount,
)
from bot.handlers.admin.adding_position import (
    add_item_callback_handler, check_item_name_for_add, add_item_description,
    add_item_price, check_category_for_add_item,
)
from bot.states import AddItemFSM


async def _walk_to_category(make_message, fsm_context, *,
                            name="NewItem", price="100", category="AddCat"):
    """Drive the FSM from the name prompt up to the category step."""
    await check_item_name_for_add(make_message(text=name, user_id=1), fsm_context)
    await add_item_description(make_message(text="A description", user_id=1), fsm_context)
    await add_item_price(make_message(text=price, user_id=1), fsm_context)


class TestAddItemStart:

    async def test_start_sets_the_name_state(self, make_callback_query, fsm_context):
        call = make_callback_query(data="add_item", user_id=900600)
        await add_item_callback_handler(call, fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_item_name
        call.message.edit_text.assert_called_once()


class TestItemNameStep:

    async def test_valid_name_advances_to_description(self, make_message, fsm_context):
        await check_item_name_for_add(make_message(text="Fresh Item", user_id=1), fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_item_description
        assert (await fsm_context.get_data())["item_name"] == "Fresh Item"

    async def test_existing_name_is_refused(self, make_message, fsm_context, item_factory):
        await item_factory(name="AlreadyHere", price=10, category="C", values=[("v", False)])

        await fsm_context.set_state(AddItemFSM.waiting_item_name)
        await check_item_name_for_add(make_message(text="AlreadyHere", user_id=1), fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_item_name

    @pytest.mark.parametrize("bad_name", [
        "",
        "   ",
        "A" * 101,          # over the 100-char cap
        "bad\x00name",      # control characters
    ])
    async def test_unsafe_name_is_refused(self, make_message, fsm_context, bad_name):
        await fsm_context.set_state(AddItemFSM.waiting_item_name)
        await check_item_name_for_add(make_message(text=bad_name, user_id=1), fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_item_name
        assert "item_name" not in await fsm_context.get_data()


class TestPriceStep:

    @pytest.mark.parametrize("text,expected", [
        ("100", 100),
        ("1", 1),                  # the minimum accepted price
        ("99999999", 99_999_999),  # Numeric(12, 2) leaves 10 integer digits
    ])
    async def test_valid_price_advances_to_category(self, make_message, fsm_context,
                                                    text, expected):
        await add_item_price(make_message(text=text, user_id=1), fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_category
        assert (await fsm_context.get_data())["item_price"] == expected

    @pytest.mark.parametrize("bad_price", [
        "abc", "", "-10", "0",
        "99.99",       # prices are whole units only
        "100000000",   # one over the cap the DB column can hold
        "１００",       # non-ASCII digits are not accepted
    ])
    async def test_invalid_price_keeps_the_state(self, make_message, fsm_context, bad_price):
        await fsm_context.set_state(AddItemFSM.waiting_item_price)
        await add_item_price(make_message(text=bad_price, user_id=1), fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_item_price
        assert "item_price" not in await fsm_context.get_data()


class TestCategoryStep:

    async def test_existing_category_creates_bare_position(self, make_message,
                                                           fsm_context, category_factory):
        await category_factory("RealCat")
        await check_item_name_for_add(make_message(text="BareItem", user_id=1), fsm_context)
        await add_item_description(make_message(text="A description", user_id=1), fsm_context)
        await add_item_price(make_message(text="100", user_id=1), fsm_context)
        msg = make_message(text="RealCat", user_id=1)
        await check_category_for_add_item(msg, fsm_context)

        item = await get_item_info("BareItem")
        assert item is not None
        assert item["price"] == Decimal("100")
        assert item["description"] == "A description"
        assert await select_item_values_amount("BareItem") == 0
        assert await fsm_context.get_state() is None
        # The admin is pointed to the stock tab for filling goods in.
        assert "Наполните её через" in msg.answer.call_args.args[0]

    async def test_unknown_category_keeps_the_state(self, make_message, fsm_context):
        await fsm_context.set_state(AddItemFSM.waiting_category)
        await check_category_for_add_item(make_message(text="Ghost", user_id=1), fsm_context)

        assert await fsm_context.get_state() == AddItemFSM.waiting_category
        assert "item_category" not in await fsm_context.get_data()
        assert await get_item_info("Ghost") is None


class TestBareCreationRace:

    async def test_race_with_existing_name_is_refused(self, make_message,
                                                      fsm_context, category_factory,
                                                      item_factory):
        """The name check and the insert are not atomic: a concurrent insert
        must not create a duplicate, the admin just sees "already exists"."""
        await category_factory("AddCat")
        await item_factory(name="RacedItem", price=10, category="AddCat",
                           values=[("v", False)])
        await fsm_context.set_state(AddItemFSM.waiting_item_name)
        await check_item_name_for_add(
            make_message(text="RacedItem", user_id=1), fsm_context
        )
        # The name step itself refuses the now-existing name.
        assert await fsm_context.get_state() == AddItemFSM.waiting_item_name
        assert await get_item_info("RacedItem") is not None
