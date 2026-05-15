from types import SimpleNamespace

from profile_service.main import (
    calculate_completeness_score,
    calculate_combined_score,
    calculate_primary_score,
    get_discovery_cache_key,
)


def test_calculate_completeness_score_full_profile():
    score = calculate_completeness_score(
        {
            "bio": "bio",
            "age": 25,
            "gender": "f",
            "city": "Moscow",
            "photo_urls": ["https://example.com/1.jpg"],
        }
    )
    assert score == 1.0


def test_calculate_completeness_score_partial_profile():
    score = calculate_completeness_score(
        {
            "bio": "bio",
            "age": None,
            "gender": None,
            "city": "Moscow",
            "photo_urls": [],
        }
    )
    assert score == 0.4


def test_calculate_combined_score():
    assert calculate_combined_score(80.0, 50.0) == 68.0


def test_calculate_primary_score_full():
    profile = SimpleNamespace(
        bio="bio",
        age=25,
        gender="f",
        city="Moscow",
        photo_urls=["https://example.com/1.jpg"],
    )
    assert calculate_primary_score(profile) == 100.0


def test_calculate_primary_score_without_photo():
    profile = SimpleNamespace(
        bio="bio",
        age=25,
        gender="f",
        city="Moscow",
        photo_urls=[],
    )
    assert calculate_primary_score(profile) == 80.0


def test_get_discovery_cache_key():
    assert get_discovery_cache_key(42) == "dating:discovery:42:queue"
