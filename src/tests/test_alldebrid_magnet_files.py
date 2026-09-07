"""Regression coverage for AllDebrid's dedicated magnet/files endpoint."""

from unittest.mock import MagicMock

from program.services.downloaders.alldebrid import AllDebridDownloader


class _Response:
    ok = True

    def json(self) -> dict:
        return {
            "status": "success",
            "data": {
                "magnets": [
                    {
                        "id": 123,
                        "files": [
                            {
                                "n": "Season 01",
                                "e": [
                                    {
                                        "n": "Episode 01.mkv",
                                        "s": 100,
                                        "l": "https://cdn.example/episode-01",
                                    }
                                ],
                            },
                            {
                                "n": "Movie.mkv",
                                "s": 200,
                                "l": "https://cdn.example/movie",
                            },
                        ],
                    }
                ]
            },
        }


def test_get_magnet_files_flattens_tree_and_preserves_leaf_links() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.api = MagicMock()
    downloader.api.session.post.return_value = _Response()
    downloader._maybe_backoff = MagicMock()

    files = downloader._get_magnet_files(123)

    assert files is not None
    assert [(file.n, file.l) for file in files] == [
        ("Episode 01.mkv", "https://cdn.example/episode-01"),
        ("Movie.mkv", "https://cdn.example/movie"),
    ]
    downloader.api.session.post.assert_called_once_with(
        "v4/magnet/files", data={"id": [123]}
    )
