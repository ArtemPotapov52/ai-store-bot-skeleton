from aiogram.filters.state import StatesGroup, State


class GoodsFSM(StatesGroup):
    """FSM for position (goods) and items management scenarios."""
    waiting_item_name_delete = State()
    waiting_item_name_show = State()
    waiting_bought_item_id = State()


class AddItemFSM(StatesGroup):
    """
    FSM for step-by-step creation of a bare position (product card):
    1) name,
    2) description,
    3) price,
    4) category — the position is created here with zero stock.

    Stock ("what the buyer receives") is added afterwards through
    “Add goods to a position”; an infinite delivery value is set
    through “Edit position” instead.
    """
    waiting_item_name = State()
    waiting_item_description = State()
    waiting_item_price = State()
    waiting_category = State()


class UpdateItemFSM(StatesGroup):
    """
    FSM for updating an item:
    1) Add item values (stock) to an existing position.
    2) Full update (name, description, price, infinity/regular, values).
    """
    # Add values to an item
    waiting_item_name_for_amount_upd = State()
    waiting_item_values_upd = State()

    # Full update
    waiting_item_name_for_update = State()
    waiting_item_new_name = State()
    waiting_item_description = State()
    waiting_item_price = State()
    waiting_make_infinity = State()
    waiting_single_value = State()
    waiting_multiple_values = State()
