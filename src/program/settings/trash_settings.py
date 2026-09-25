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
    min_score: int = Field(
        default=-1000,
        description="Global minimum TRaSH score threshold; releases scoring lower are rejected",
    )
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
