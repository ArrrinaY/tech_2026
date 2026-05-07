from profile_service.main import (
    calculate_completeness_score,
    calculate_combined_score,
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


def test_get_discovery_cache_key():
    assert get_discovery_cache_key(42) == "dating:discovery:42:queue"
