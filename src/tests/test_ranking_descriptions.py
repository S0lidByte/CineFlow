"""Tests for RTN ranking schema description enrichment."""

from program.settings.ranking_descriptions import (
    DENY_KEY_HELP,
    enrich_ranking_schema,
)


def test_enrich_ranking_schema_injects_audio_ddp_description() -> None:
    schema = {
        "properties": {"ranking": {"type": "object"}},
        "$defs": {
            "AudioRankModel": {
                "type": "object",
                "properties": {
                    "dolby_digital_plus": {"type": "object"},
                },
            },
            "CustomRank": {
                "type": "object",
                "properties": {
                    "fetch": {"type": "boolean"},
                    "use_custom_rank": {"type": "boolean"},
                    "rank": {"type": "integer"},
                },
            },
            "OptionsConfig": {
                "type": "object",
                "properties": {
                    "remove_all_trash": {"type": "boolean"},
                },
            },
        },
    }

    enrich_ranking_schema(schema)

    audio = schema["$defs"]["AudioRankModel"]["properties"]["dolby_digital_plus"]
    assert "audio_dolby_digital_plus" in audio["description"]
    assert audio["title"] == "Dolby Digital Plus (DDP)"

    fetch = schema["$defs"]["CustomRank"]["properties"]["fetch"]
    assert "denied by:" in fetch["description"]

    trash_opt = schema["$defs"]["OptionsConfig"]["properties"]["remove_all_trash"]
    assert "trash" in trash_opt["description"].lower()

    ranking = schema["properties"]["ranking"]
    assert "denied by:" in ranking["description"]


def test_deny_key_catalog_covers_common_log_reasons() -> None:
    for key in (
        "audio_dolby_digital_plus",
        "audio_dolby_digital",
        "quality_remux",
        "extras_dubbed",
        "extras_site",
        "rips_dvdrip",
        "trash_size",
    ):
        assert key in DENY_KEY_HELP
