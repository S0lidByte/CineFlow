"""Redaction and round-trip preservation for credential-bearing settings paths."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT_API_KEY_SENTINEL = "********"
SETTINGS_SECRET_SENTINEL: str = "********-settings"  # noqa: S105

# Explicit paths avoid classifying public identifiers (for example Trakt's client
# ID) as secrets merely because a field is named ``api_key``.
SECRET_SETTING_PATHS: tuple[str, ...] = (
    "api_key",
    "downloaders.real_debrid.api_key",
    "downloaders.debrid_link.api_key",
    "downloaders.all_debrid.api_key",
    "updaters.plex.token",
    "updaters.jellyfin.api_key",
    "updaters.emby.api_key",
    "content.listrr.api_key",
    "content.mdblist.api_key",
    "content.overseerr.api_key",
    "content.overseerr.webhook_secret",
    "content.plex_webhook.webhook_secret",
    "content.trakt.oauth.oauth_client_secret",
    "content.trakt.oauth.access_token",
    "content.trakt.oauth.refresh_token",
    "scraping.orionoid.api_key",
    "scraping.jackett.api_key",
    "scraping.prowlarr.api_key",
    "scraping.aiostreams.uuid",
    "scraping.aiostreams.password",
    "subtitle.providers.opensubtitles.password",
    "subtitle.providers.subdl.api_key",
)

# URL-valued settings can embed credentials in userinfo or query parameters.
# Keep their URL shape and redact only sensitive components so validation and
# non-secret URL configuration continue to work.
SECRET_SETTING_URL_PATHS: tuple[str, ...] = (
    "database.host",
    "downloaders.proxy_url",
    "content.trakt.proxy_url",
    "updaters.plex.url",
    "updaters.jellyfin.url",
    "updaters.emby.url",
    "content.overseerr.url",
    "scraping.torrentio.url",
    "scraping.comet.url",
    "scraping.zilean.url",
    "scraping.mediafusion.url",
    "scraping.jackett.url",
    "scraping.prowlarr.url",
    "scraping.rarbg.url",
    "scraping.stremthru.url",
    "scraping.aiostreams.url",
)

# URL-valued lists can embed auth in query strings; preserve list shape and
# restore unchanged entries from the current server-side settings on save.
SETTINGS_SECRET_URL_LIST_PATHS: tuple[str, ...] = (
    "notifications.service_urls",
    "content.plex_watchlist.rss",
)


def _redact_url_credentials(value: str) -> str:
    try:
        parsed = urlsplit(value)
        netloc = parsed.netloc
        userinfo, separator, host = parsed.netloc.rpartition("@")
        if separator:
            username, colon, password = userinfo.partition(":")
            username = "redacted" if username else ""
            if colon:
                password = "redacted" if password else ""
                userinfo = f"{username}:{password}"
            else:
                userinfo = username
            netloc = f"{userinfo}@{host}"
        sensitive_names = {
            "apikey",
            "api_key",
            "token",
            "access_token",
            "refresh_token",
            "client_secret",
            "password",
            "secret",
            "webhook_secret",
            "x-webhook-secret",
            "x-plex-token",
        }
        query = [
            (key, "redacted" if key.lower() in sensitive_names else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                urlencode(query, doseq=True),
                parsed.fragment,
            )
        )
    except Exception:
        return "[redacted-url]"


def _redact_sensitive_url_list(value: str) -> str:
    return _redact_url_credentials(value)


def _restore_redacted_url_credentials(current: str, submitted: str) -> str:
    """Restore unchanged URL credentials while keeping edited URL components."""
    old = urlsplit(current)
    new = urlsplit(submitted)

    old_userinfo, old_separator, _ = old.netloc.rpartition("@")
    new_userinfo, new_separator, new_host = new.netloc.rpartition("@")
    netloc = new.netloc
    if old_separator and new_separator:
        old_parts = old_userinfo.split(":")
        new_parts = new_userinfo.split(":")
        restored = [
            old_parts[index] if part == "redacted" and index < len(old_parts) else part
            for index, part in enumerate(new_parts)
        ]
        netloc = f"{':'.join(restored)}@{new_host}"

    old_query = parse_qsl(old.query, keep_blank_values=True)
    new_query = parse_qsl(new.query, keep_blank_values=True)
    old_by_key: dict[str, list[str]] = {}
    for key, value in old_query:
        old_by_key.setdefault(key, []).append(value)
    seen: dict[str, int] = {}
    restored_query: list[tuple[str, str]] = []
    for key, value in new_query:
        index = seen.get(key, 0)
        seen[key] = index + 1
        old_values = old_by_key.get(key, [])
        if value == "redacted" and index < len(old_values):
            value = old_values[index]
        restored_query.append((key, value))

    return urlunsplit(
        (
            new.scheme,
            netloc,
            new.path,
            urlencode(restored_query, doseq=True),
            new.fragment,
        )
    )


def _get_path(root: dict[str, Any], path: str) -> tuple[dict[str, Any], str] | None:
    parts = path.split(".")
    parent = root
    for part in parts[:-1]:
        value = parent.get(part)
        if not isinstance(value, dict):
            return None
        parent = cast(dict[str, Any], value)
    return parent, parts[-1]


def redact_settings_secrets(settings: dict[str, Any]) -> dict[str, Any]:
    """Redact explicitly classified secret values in a settings payload."""
    for path in SECRET_SETTING_PATHS:
        location = _get_path(settings, path)
        if location is not None:
            parent, key = location
            if parent.get(key):
                parent[key] = (
                    ROOT_API_KEY_SENTINEL
                    if path == "api_key"
                    else SETTINGS_SECRET_SENTINEL
                )

    for path in SECRET_SETTING_URL_PATHS:
        location = _get_path(settings, path)
        if location is not None:
            parent, key = location
            value = parent.get(key)
            if isinstance(value, str) and value:
                parent[key] = _redact_url_credentials(value)
            elif path == "database.host" and value is not None:
                parent[key] = _redact_url_credentials(str(value))

    for path in SETTINGS_SECRET_URL_LIST_PATHS:
        location = _get_path(settings, path)
        if location is not None:
            parent, key = location
            value = parent.get(key)
            if isinstance(value, list):
                items = cast(list[Any], value)
                parent[key] = [
                    _redact_sensitive_url_list(item) if isinstance(item, str) else item
                    for item in items
                ]
    return settings


def preserve_redacted_settings_secrets(
    current: dict[str, Any], submitted: dict[str, Any]
) -> dict[str, Any]:
    """Restore masked fields from current settings; explicit values remain updates."""
    for path in SECRET_SETTING_PATHS:
        current_location = _get_path(current, path)
        submitted_location = _get_path(submitted, path)
        if current_location is None or submitted_location is None:
            continue
        current_parent, key = current_location
        submitted_parent, _ = submitted_location
        if path == "api_key":
            replacement = ROOT_API_KEY_SENTINEL
        else:
            replacement = SETTINGS_SECRET_SENTINEL
        if submitted_parent.get(key) == replacement:
            submitted_parent[key] = current_parent.get(key, "")

    # Restore URL credentials when the submitted value is exactly the redacted
    # version returned by GET; edits to non-secret URL components remain updates.
    for path in SECRET_SETTING_URL_PATHS:
        current_location = _get_path(current, path)
        submitted_location = _get_path(submitted, path)
        if current_location is None or submitted_location is None:
            continue
        current_parent, key = current_location
        submitted_parent, _ = submitted_location
        current_value = current_parent.get(key)
        submitted_value = submitted_parent.get(key)
        if path == "database.host" and current_value is not None:
            current_text = str(current_value)
            if isinstance(submitted_value, str) and submitted_value != current_text:
                submitted_parent[key] = _restore_redacted_url_credentials(
                    current_text, submitted_value
                )
        elif (
            isinstance(current_value, str)
            and isinstance(submitted_value, str)
            and submitted_value != current_value
        ):
            submitted_parent[key] = _restore_redacted_url_credentials(
                current_value, submitted_value
            )

    # URL-valued lists can also contain credentials; preserve each unchanged entry.
    for path in SETTINGS_SECRET_URL_LIST_PATHS:
        current_location = _get_path(current, path)
        submitted_location = _get_path(submitted, path)
        if current_location is None or submitted_location is None:
            continue
        current_parent, key = current_location
        submitted_parent, _ = submitted_location
        old_values = current_parent.get(key)
        new_values = submitted_parent.get(key)
        if isinstance(old_values, list) and isinstance(new_values, list):
            old_items = cast(list[Any], old_values)
            new_items = cast(list[Any], new_values)
            submitted_parent[key] = [
                (
                    _restore_redacted_url_credentials(old_items[index], value)
                    if (
                        index < len(old_items)
                        and isinstance(old_items[index], str)
                        and isinstance(value, str)
                        and value != old_items[index]
                    )
                    else value
                )
                for index, value in enumerate(new_items)
            ]
    return submitted
