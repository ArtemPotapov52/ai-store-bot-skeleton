from aiogram.filters.state import StatesGroup, State


class CategoryFSM(StatesGroup):
    """
    FSM states for category management:
    - add,
    - delete,
    - rename.
    """
    waiting_add_category = State()
    waiting_delete_category = State()
    waiting_update_category = State()
    waiting_update_category_name = State()
