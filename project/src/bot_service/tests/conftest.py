import pytest_asyncio

import bot_service.main as bot_main

_ORIGINAL_GET_HTTP_CLIENT = bot_main.get_http_client


@pytest_asyncio.fixture(autouse=True)
async def reset_bot_globals():
    """Между тестами закрываем httpx-клиент и очищаем кэши бота (иначе моки и счётчики путаются)."""
    bot_main.get_http_client = _ORIGINAL_GET_HTTP_CLIENT
    await bot_main.close_http_client()
    bot_main.user_lookup_cache.clear()
    bot_main.last_search_action_ts.clear()
    bot_main.recent_rate_actions.clear()
    bot_main.user_action_locks.clear()
    yield
    await bot_main.close_http_client()
    bot_main.user_lookup_cache.clear()
    bot_main.last_search_action_ts.clear()
    bot_main.recent_rate_actions.clear()
    bot_main.user_action_locks.clear()
    bot_main.get_http_client = _ORIGINAL_GET_HTTP_CLIENT
