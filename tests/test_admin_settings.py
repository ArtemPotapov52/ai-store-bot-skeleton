from bot.web.admin import _persist_pay_currency


def test_persist_pay_currency_changes_only_setting(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TOKEN=keep-me\nPAY_CURRENCY=RUB\nCRYPTO_PAY_TOKEN=keep-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))

    assert _persist_pay_currency("USD") is True
    assert env_file.read_text(encoding="utf-8") == (
        "TOKEN=keep-me\nPAY_CURRENCY=USD\nCRYPTO_PAY_TOKEN=keep-secret\n"
    )


def test_persist_pay_currency_appends_missing_setting(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=keep-me", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))

    assert _persist_pay_currency("EUR") is True
    assert env_file.read_text(encoding="utf-8") == "TOKEN=keep-me\nPAY_CURRENCY=EUR\n"
