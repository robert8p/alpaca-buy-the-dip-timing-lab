import pytest

from app.config import Settings


def test_default_feed_is_supported(monkeypatch):
    monkeypatch.delenv("ALPACA_FEED", raising=False)
    assert Settings().alpaca_feed in {"sip", "iex"}


def test_web_settings_reject_missing_or_weak_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("APP_PASSWORD", "too-short")
    monkeypatch.setenv("SESSION_SECRET", "also-too-short")
    with pytest.raises(RuntimeError, match="APP_PASSWORD"):
        Settings().validate_web()


def test_web_settings_accept_minimum_secure_shape(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("APP_PASSWORD", "123456789012")
    monkeypatch.setenv("SESSION_SECRET", "12345678901234567890123456789012")
    Settings().validate_web()


def test_worker_settings_reject_invalid_feed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("ALPACA_FEED", "unknown")
    with pytest.raises(RuntimeError, match="ALPACA_FEED"):
        Settings().validate_worker()
