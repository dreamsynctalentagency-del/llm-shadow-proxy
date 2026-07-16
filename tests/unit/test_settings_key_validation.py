"""Guarantee that dummy / missing DO_INFERENCE_API_KEY values are rejected.

These tests are the safety net that prevents a bad deployment from silently
sending garbage to DigitalOcean.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from shadow_proxy.llm.do_inference import DOInferenceClient
from shadow_proxy.settings import Settings


def _s(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "do_inference_api_key": SecretStr("a" * 40),  # plausible real length
        "shadow_proxy_allow_dummy_key": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestSettingsRuntimeReady:
    def test_valid_key_passes(self) -> None:
        _s().assert_runtime_ready()

    def test_empty_key_rejected(self) -> None:
        s = _s(do_inference_api_key=SecretStr(""))
        with pytest.raises(RuntimeError, match="DO_INFERENCE_API_KEY is not set"):
            s.assert_runtime_ready()

    @pytest.mark.parametrize(
        "dummy",
        [
            "fake",
            "fake-key",
            "FAKE-KEY-1234567890",
            "dummy-token-please-replace",
            "your-key-here-1234567890",
            "changeme-1234567890abcdef",
            "PLACEHOLDER-1234567890",
            "paste-your-key-here",
            "todo-generate-real-key",
            "xxxxxxxxxxxxxxxxxxxx",
        ],
    )
    def test_dummy_key_rejected(self, dummy: str) -> None:
        s = _s(do_inference_api_key=SecretStr(dummy))
        with pytest.raises(RuntimeError, match="placeholder"):
            s.assert_runtime_ready()

    def test_short_key_rejected(self) -> None:
        s = _s(do_inference_api_key=SecretStr("short"))
        with pytest.raises(RuntimeError, match="too short"):
            s.assert_runtime_ready()

    def test_allow_dummy_key_bypass(self) -> None:
        s = _s(
            do_inference_api_key=SecretStr("fake"),
            shadow_proxy_allow_dummy_key=True,
        )
        s.assert_runtime_ready()  # does not raise

    def test_spaces_requires_creds_when_selected(self) -> None:
        s = _s(raw_store_type="spaces")
        with pytest.raises(RuntimeError, match="RAW_STORE_TYPE=spaces"):
            s.assert_runtime_ready()

    def test_spaces_ok_when_all_creds_set(self) -> None:
        s = _s(
            raw_store_type="spaces",
            do_spaces_bucket="bkt",
            do_spaces_key="ak",
            do_spaces_secret=SecretStr("sk"),
        )
        s.assert_runtime_ready()


class TestSecretsDoNotLeak:
    def test_secretstr_masked_in_repr(self) -> None:
        s = _s()
        assert "aaaa" not in repr(s)

    def test_secretstr_masked_in_model_dump(self) -> None:
        s = _s()
        dumped = s.model_dump()
        assert "aaaa" not in str(dumped)

    def test_redacted_summary_never_shows_value(self) -> None:
        s = _s()
        summary = s.redacted_key_summary()
        assert "aaaa" not in summary
        assert summary == "length=40"

    def test_redacted_summary_for_unset(self) -> None:
        s = _s(do_inference_api_key=SecretStr(""))
        assert s.redacted_key_summary() == "unset"


class TestDOInferenceClientGuard:
    """Defense-in-depth: even bypassing Settings, the client refuses bad keys."""

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="required"):
            DOInferenceClient(base_url="https://x", api_key="")

    def test_dummy_rejected(self) -> None:
        with pytest.raises(ValueError, match="placeholder"):
            DOInferenceClient(base_url="https://x", api_key="fake-key-that-is-long-enough")

    def test_short_rejected(self) -> None:
        with pytest.raises(ValueError, match="length"):
            DOInferenceClient(base_url="https://x", api_key="short")

    def test_plausible_key_accepted(self) -> None:
        # Should not raise; the client is constructed but no request is made.
        DOInferenceClient(base_url="https://x", api_key="A" * 40)
