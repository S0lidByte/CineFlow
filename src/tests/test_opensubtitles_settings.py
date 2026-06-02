import pytest

from program.settings.models import AppModel, OpenSubtitlesProviderConfig


def test_opensubtitles_provider_defaults_to_allow_anonymous():
    config = dict(
        post_processing=dict(
            subtitle=dict(
                providers=dict(
                    opensubtitles=dict(
                        enabled=True,
                    )
                )
            )
        )
    )
    validated = AppModel.model_validate(config)

    assert validated.post_processing.subtitle.providers.opensubtitles.allow_anonymous is True


def test_opensubtitles_provider_requires_credentials_without_anonymous():
    config = dict(
        post_processing=dict(
            subtitle=dict(
                providers=dict(
                    opensubtitles=dict(
                        enabled=True,
                        allow_anonymous=False,
                    )
                )
            )
        )
    )

    with pytest.raises(Exception):
        AppModel.model_validate(config)


def test_opensubtitles_provider_model_defaults_to_allow_anonymous():
    validated = OpenSubtitlesProviderConfig(enabled=True)
    assert validated.allow_anonymous is True
