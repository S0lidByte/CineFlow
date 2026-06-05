import re

from unittest.mock import Mock

from program import utils


def test_generate_api_key_generates_and_redacts_secret(monkeypatch):
    monkeypatch.setenv("API_KEY", "short-key")

    monkeypatch.setattr(utils.secrets, "choice", lambda _: "A")
    warning = Mock()
    monkeypatch.setattr(utils.logger, "warning", warning)

    api_key = utils.generate_api_key()

    assert api_key == "A" * 32
    assert warning.call_count == 2

    new_key_call = next(
        (
            call
            for call in warning.call_args_list
            if "New api key generated" in str(call.args[0])
        ),
        None,
    )
    assert new_key_call is not None

    # Ensure only non-sensitive fragments are logged.
    assert new_key_call.args[1] == 32
    assert new_key_call.args[2] == "AAAA"
    assert new_key_call.args[3] == "AAAA"

    logged_messages = [str(call.args[0]) for call in warning.call_args_list]
    assert not any(re.search(r"\b[A-Za-z0-9]{32}\b", message) for message in logged_messages)


def test_generate_api_key_uses_existing_key(monkeypatch):
    existing_key = "A" * 32
    monkeypatch.setenv("API_KEY", existing_key)

    warning = Mock()
    monkeypatch.setattr(utils.logger, "warning", warning)

    assert utils.generate_api_key() == existing_key
    warning.assert_not_called()
