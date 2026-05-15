"""
Тесты клиента бота к Profile Service: функции из bot_service/main.py с httpx.MockTransport.

Покрывают сетевой слой бота без Telegram и без живого API.
"""
from __future__ import annotations

import httpx
import pytest

import bot_service.main as m


def _install_mock_client(handler) -> None:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=m.HTTP_TIMEOUT)

    def _get() -> httpx.AsyncClient:
        return client

    m.get_http_client = _get  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_register_user_returns_json_on_201():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/users/register"
        return httpx.Response(
            201,
            json={"id": 1, "telegram_id": 999001, "username": "u1", "first_name": "Ann"},
        )

    _install_mock_client(handler)
    out = await m.register_user_in_profile_service(999001, "u1", "Ann")
    assert out == {"id": 1, "telegram_id": 999001, "username": "u1", "first_name": "Ann"}


@pytest.mark.asyncio
async def test_register_user_returns_json_on_409():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"id": 1, "telegram_id": 999001, "username": "u1", "first_name": "Ann"},
        )

    _install_mock_client(handler)
    out = await m.register_user_in_profile_service(999001, "u1", "Ann")
    assert out["telegram_id"] == 999001


@pytest.mark.asyncio
async def test_register_user_returns_none_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install_mock_client(handler)
    assert await m.register_user_in_profile_service(1, None, None) is None


@pytest.mark.asyncio
async def test_update_user_name_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/users/777"
        return httpx.Response(200, json={"id": 3, "telegram_id": 777, "username": None, "first_name": "New"})

    _install_mock_client(handler)
    assert await m.update_user_name_in_profile_service(777, "New") is True


@pytest.mark.asyncio
async def test_update_user_name_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    _install_mock_client(handler)
    assert await m.update_user_name_in_profile_service(777, "New") is False


@pytest.mark.asyncio
async def test_get_user_from_profile_service_200_and_cache():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.method == "GET"
        assert request.url.path == "/api/v1/users/555"
        return httpx.Response(200, json={"id": 2, "telegram_id": 555, "username": None, "first_name": "Bob"})

    _install_mock_client(handler)
    a = await m.get_user_from_profile_service(555)
    b = await m.get_user_from_profile_service(555)
    assert a == b
    assert a["telegram_id"] == 555
    assert len(calls) == 1, "второй раз должен отдаться из кэша бота"


@pytest.mark.asyncio
async def test_get_user_from_profile_service_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    _install_mock_client(handler)
    assert await m.get_user_from_profile_service(404404) is None


@pytest.mark.asyncio
async def test_get_profile_from_profile_service():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/profiles/10"
        return httpx.Response(
            200,
            json={
                "id": 5,
                "user_id": 10,
                "bio": "hi",
                "interests": None,
                "photo_urls": [],
                "completeness_score": 0.5,
                "age": 25,
                "gender": "f",
                "city": "MSK",
            },
        )

    _install_mock_client(handler)
    p = await m.get_profile_from_profile_service(10)
    assert p is not None and p["city"] == "MSK"


@pytest.mark.asyncio
async def test_get_next_discovery_profile_200_and_404():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/discovery/3/next"
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "profile_id": 9,
                    "user_id": 8,
                    "first_name": "X",
                    "age": 30,
                    "gender": "m",
                    "city": "SPB",
                    "bio": "",
                    "photo_urls": [],
                    "rating": {"primary_score": 50, "behavioral_score": 40, "combined_score": 46},
                },
            )
        return httpx.Response(404, text="empty")

    _install_mock_client(handler)
    first = await m.get_next_discovery_profile(3)
    assert first is not None and first["profile_id"] == 9
    second = await m.get_next_discovery_profile(3)
    assert second is None


@pytest.mark.asyncio
async def test_send_interaction_to_profile_service():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/interactions"
        return httpx.Response(200, json={"status": "ok", "is_match": False})

    _install_mock_client(handler)
    out = await m.send_interaction_to_profile_service(1, 99, "like")
    assert out is not None and out["is_match"] is False


@pytest.mark.asyncio
async def test_get_matches_200_and_404_empty():
    def handler200(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/matches/7"
        return httpx.Response(200, json=[{"user_id": 2, "first_name": "Z"}])

    _install_mock_client(handler200)
    m1 = await m.get_matches_from_profile_service(7)
    assert m1 == [{"user_id": 2, "first_name": "Z"}]

    def handler404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _install_mock_client(handler404)
    m2 = await m.get_matches_from_profile_service(7)
    assert m2 == []


@pytest.mark.asyncio
async def test_save_profile_data():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        return httpx.Response(200, json={})

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="validation")

    _install_mock_client(ok)
    assert await m.save_profile_data(1, {"bio": "x"}) is True

    _install_mock_client(bad)
    assert await m.save_profile_data(1, {"bio": "x"}) is False


@pytest.mark.asyncio
async def test_upload_profile_photo():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/api/v1/profiles/4/photos" in str(request.url)
        return httpx.Response(200, json={"ok": True})

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="big")

    _install_mock_client(ok)
    assert await m.upload_profile_photo_to_profile_service(4, "a.jpg", b"\xff\xd8", "image/jpeg") is True

    _install_mock_client(bad)
    assert await m.upload_profile_photo_to_profile_service(4, "a.jpg", b"x", "image/jpeg") is False


def test_prioritize_photo_urls_prefers_minio_host():
    urls = ["https://cdn.example.com/a.jpg", "http://localhost:9000/dating-photos/u/1.jpg"]
    out = m.prioritize_photo_urls(urls)
    assert "localhost:9000" in out[0]


def test_format_gender_russian_and_unknown():
    assert m.format_gender("female") == "Женский"
    assert m.format_gender("MALE") == "Мужской"
    assert m.format_gender(None) == "Не указан"
    assert m.format_gender("alien") == "alien"


def test_is_skip_command_variants():
    assert m.is_skip_command("пропустить") is True
    assert m.is_skip_command("SKIP") is True
    assert m.is_skip_command("  дальше  ") is True
    assert m.is_skip_command("hello") is False


def test_should_rate_limit_search_action():
    m.last_search_action_ts.clear()
    uid = 4242
    assert m.should_rate_limit_search_action(uid) is False
    assert m.should_rate_limit_search_action(uid) is True


def test_is_duplicate_rate_action():
    m.recent_rate_actions.clear()
    assert m.is_duplicate_rate_action(1, 100, "like") is False
    assert m.is_duplicate_rate_action(1, 100, "like") is True


def test_get_search_keyboard_contains_profile_id():
    kb = m.get_search_keyboard(profile_id=12345)
    row0 = kb.inline_keyboard[0]
    datas = [b.callback_data for b in row0]
    assert any("12345" in (d or "") for d in datas)
