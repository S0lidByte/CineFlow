from unittest.mock import MagicMock

from program.apis.tvdb_api import TVDBApi


def test_get_series_uses_direct_generated_response_model():
    api = TVDBApi.__new__(TVDBApi)
    api.session = MagicMock()
    api.session.get.return_value = MagicMock(
        ok=True,
        json=lambda: {
            "data": {
                "id": 42,
                "name": "Example Series",
                "status": {"id": 1, "name": "Continuing", "keepUpdated": True},
            },
            "status": "success",
        },
    )
    api._get_headers = MagicMock(return_value={})

    series = api.get_series("42")

    assert series is not None
    assert series.id == 42
    assert series.name == "Example Series"
