import pytest
from unittest.mock import patch

from bot.i18n.main import format_dt, get_locale, localize
from bot.i18n.strings import DEFAULT_LOCALE, TRANSLATIONS


@pytest.fixture(autouse=True)
def clear_locale_cache():
    """get_locale is lru_cached — every case needs a cold cache on both sides."""
    get_locale.cache_clear()
    yield
    get_locale.cache_clear()


def _with_locale(value):
    """Patch the configured locale for one call."""
    return patch('bot.i18n.main.EnvKeys', **{"BOT_LOCALE": value})


class TestGetLocale:

    @pytest.mark.parametrize("configured,expected", [
        ("ru", "ru"),
        ("  RU  ", "ru"),          # stripped and lowered
        ("vi", "vi"),
        ("xx", DEFAULT_LOCALE),    # unknown locale falls back
    ])
    def test_resolution(self, configured, expected):
        with _with_locale(configured):
            assert get_locale() == expected


class TestLocalize:

    @pytest.mark.parametrize("locale,expected", [
        ("ru", "купить"),
        ("en", "buy"),
        ("vi", "mua"),
    ])
    def test_catalog_categories_screen_explains_shopping(self, locale, expected):
        with _with_locale(locale):
            result = localize("shop.categories.title", shop_icon="🛍")

        assert expected in result.lower()
        assert "{shop_icon}" not in result

    @pytest.mark.parametrize("locale", ["ru", "en", "vi"])
    @pytest.mark.parametrize("key, extra", [
        ("shop.goods.button.in_stock", {"count": 5}),
        ("shop.goods.button.unlimited", {}),
        ("shop.goods.button.out", {}),
        ("shop.goods.button.note", {"note": "предзаказ ⏳"}),
    ])
    def test_catalog_buttons_show_price_before_stock_status(self, locale, key, extra):
        with _with_locale(locale):
            result = localize(
                key,
                name="ChatGPT K12 EDU 2 ГОДА",
                price="799",
                currency="RUB",
                **extra,
            )

        assert result.startswith("ChatGPT K12 EDU 2 ГОДА | 799 RUB | ")

    def test_existing_key(self):
        with _with_locale("ru"):
            # Returns the translation, not the key itself.
            assert localize("btn.shop") != "btn.shop"

    def test_missing_key_returns_key(self):
        with _with_locale("ru"):
            assert localize("nonexistent.key.that.does.not.exist") \
                   == "nonexistent.key.that.does.not.exist"

    def test_format_with_kwargs(self):
        with _with_locale("ru"):
            result = localize(
                "menu.title", id=12345, name="TestUser", shop_name="Test Shop"
            )
        assert "12345" in result
        assert "TestUser" in result

    def test_format_error_returns_unformatted(self):
        # menu.title expects several fields — wrong kwargs must not crash.
        with _with_locale("ru"):
            result = localize("menu.title", wrong_key="value")
        assert "{id}" in result and "{name}" in result

    def test_localize_returns_nonempty_string(self):
        with _with_locale("ru"):
            result = localize("btn.back")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_vietnamese_menu_renders(self):
        with _with_locale("vi"):
            result = localize(
                "menu.title", id=12345, name="TestUser", shop_name="Test Shop"
            )
        assert "12345" in result
        assert "TestUser" in result
        assert "{id}" not in result


class TestLocaleParity:
    def test_all_locales_share_keys_and_placeholders(self):
        import string

        def fields(text):
            return sorted(
                {f for _, f, _, _ in string.Formatter().parse(text) if f is not None}
            )

        base = TRANSLATIONS[DEFAULT_LOCALE]
        for locale, table in TRANSLATIONS.items():
            assert set(table) == set(base), f"key mismatch in {locale}"
            for key, template in base.items():
                assert fields(table[key]) == fields(template), (
                    f"placeholder mismatch in {locale}:{key}"
                )


class TestFormatDt:
    def test_datetime_is_shortened(self):
        import datetime
        assert format_dt(datetime.datetime(2026, 9, 16, 19, 13, 2)) == "16.09.2026 19:13"

    def test_iso_string_with_tz_is_normalized(self):
        assert format_dt("2026-09-16T19:13:02.866172+00:00") == "16.09.2026 19:13"

    def test_garbage_passes_through(self):
        assert format_dt("not-a-date") == "not-a-date"
        assert format_dt(None) == ""
