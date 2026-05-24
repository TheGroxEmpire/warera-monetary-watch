from warera_monetary_watch.config import normalize_base_path


def test_normalize_base_path_handles_root() -> None:
    assert normalize_base_path("/") == "/"


def test_normalize_base_path_adds_leading_slash() -> None:
    assert normalize_base_path("monetary-watch/") == "/monetary-watch"

