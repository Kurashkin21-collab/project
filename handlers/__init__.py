from aiogram import Dispatcher
from .onboarding  import router as onboarding_router
from .transactions import router as transactions_router
from .plan         import router as plan_router
from .budget       import router as budget_router
from .nutrition    import router as nutrition_router


def register_handlers(dp: Dispatcher):
    dp.include_router(onboarding_router)
    dp.include_router(transactions_router)
    dp.include_router(plan_router)
    dp.include_router(budget_router)
    dp.include_router(nutrition_router)
