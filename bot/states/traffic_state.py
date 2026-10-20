from aiogram.fsm.state import State, StatesGroup


class TrafficFSM(StatesGroup):
    waiting_campaign_name = State()
