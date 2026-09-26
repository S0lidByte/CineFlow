"""TRaSH settings model integrated into CineFlow settings hierarchy."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from program.settings.migratable import MigratableBaseModel
from program.settings.trash_catalog import (
    get_default_trash_custom_formats,
    get_default_trash_profiles,
)
from program.settings.trash_models import TrashCustomFormat, TrashProfile


class TrashSettingsModel(MigratableBaseModel):
    """Configuration for TRaSH Guides Custom Formats scoring and quality filters."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    enabled: bool = Field(
        default=False,
        description="Enable TRaSH Guides custom format scoring engine in RTN scraping pipeline",
    )
    active_profile: str = Field(
        default="trash_balanced",
        description="Active TRaSH profile preset id (trash_balanced, trash_remux, trash_webdl, trash_anime)",
    )
    movie_profile: str | None = Field(
        default=None,
        description="Profile ID specifically for movies. Falls back to active_profile if None.",
    )
    show_profile: str | None = Field(
        default=None,
        description="Profile ID specifically for TV shows. Falls back to active_profile if None.",
    )
    anime_profile: str | None = Field(
        default=None,
        description="Profile ID specifically for anime. Falls back to active_profile if None.",
    )
    min_score: int = Field(
        default=-1000,
        description="Global minimum TRaSH score threshold; releases scoring lower are rejected",
    )

    @property
    def min_score_threshold(self) -> int:
        return self.min_score

    @min_score_threshold.setter
    def min_score_threshold(self, value: int) -> None:
        self.min_score = value

    reject_negative_scores: bool = Field(
        default=False,
        description="Reject any release with a negative aggregate TRaSH score (strict quality filter)",
    )
    reject_unwanted_sources: bool = Field(
        default=True,
        description="Instantly reject unreleased CAM/Telesync/Screener sources regardless of other bonus points",
    )
    custom_formats: list[TrashCustomFormat] = Field(
        default_factory=get_default_trash_custom_formats,
        description="Configured TRaSH custom format definitions and weights",
    )
    profiles: list[TrashProfile] = Field(
        default_factory=get_default_trash_profiles,
        description="Saved TRaSH scoring profiles",
    )

    def get_active_profile(self) -> TrashProfile | None:
        """Find the currently selected active profile."""
        for prof in self.profiles:
            if prof.profile_id == self.active_profile:
                return prof
        return None

    def get_profile(self, profile_id: str | None) -> TrashProfile | None:
        """Find a profile by id, or active profile if not provided."""
        if not profile_id:
            return self.get_active_profile()
        for prof in self.profiles:
            if prof.profile_id == profile_id:
                return prof
        return None

    def resolve_profile(
        self,
        media_type: str | None = None,
        is_anime: bool = False,
        profile_id: str | None = None,
    ) -> TrashProfile | None:
        """Resolve the effective TRaSH scoring profile based on media type, anime status, or explicit profile_id.

        Fallback hierarchy:
        1. If profile_id is explicitly provided, look up and return that profile.
        2. If is_anime is True:
           - check anime_profile if configured
           - fallback to "trash_anime" profile if present
           - fallback to active_profile
        3. If media_type is "movie":
           - check movie_profile if configured
           - fallback to active_profile
        4. If media_type in ("show", "series", "season", "episode"):
           - check show_profile if configured
           - fallback to active_profile
        5. Fallback to active_profile.
        """
        if profile_id:
            return self.get_profile(profile_id)

        if is_anime:
            if self.anime_profile:
                prof = self.get_profile(self.anime_profile)
                if prof:
                    return prof
            anime_prof = self.get_profile("trash_anime")
            if anime_prof:
                return anime_prof
            return self.get_active_profile()

        if media_type:
            norm_type = media_type.lower().strip()
            if norm_type == "movie":
                if self.movie_profile:
                    prof = self.get_profile(self.movie_profile)
                    if prof:
                        return prof
                return self.get_active_profile()
            if norm_type in ("show", "series", "season", "episode"):
                if self.show_profile:
                    prof = self.get_profile(self.show_profile)
                    if prof:
                        return prof
                return self.get_active_profile()

        return self.get_active_profile()

    def get_profile_for_item(self, item: object | None) -> TrashProfile | None:
        """Resolve the effective profile for a given MediaItem or mock object."""
        if item is None:
            return self.get_active_profile()

        is_anime = bool(getattr(item, "is_anime", False))
        media_type = getattr(item, "type", None)
        return self.resolve_profile(media_type=media_type, is_anime=is_anime)
