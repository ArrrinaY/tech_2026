"""Минимальная проверка конфигурации окружения бота."""


def test_settings_load():
    from common.config import get_settings

    s = get_settings()
    assert s.profile_service_port == 8201
