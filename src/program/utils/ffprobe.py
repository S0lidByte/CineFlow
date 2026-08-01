import subprocess
from fractions import Fraction
from typing import Any, ClassVar, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FFProbeVideoTrack(BaseModel):
    """Model representing video track metadata"""

    codec: str | None = Field(default="", description="Codec of the video track")
    width: int = Field(default=0, description="Width of the video track")
    height: int = Field(default=0, description="Height of the video track")
    frame_rate: float = Field(
        default=0.0,
        description="Frame rate of the video track",
    )


class FFProbeAudioTrack(BaseModel):
    """Model representing audio track metadata"""

    codec: str | None = Field(default="", description="Codec of the audio track")
    channels: int = Field(
        default=0, description="Number of channels in the audio track"
    )
    sample_rate: int = Field(default=0, description="Sample rate of the audio track")
    language: str | None = Field(default="", description="Language of the audio track")


class FFProbeSubtitleTrack(BaseModel):
    """Model representing subtitle track metadata"""

    codec: str | None = Field(default="", description="Codec of the subtitle track")
    language: str | None = Field(
        default="", description="Language of the subtitle track"
    )


class FFProbeMediaMetadata(BaseModel):
    """Model representing complete media file metadata"""

    filename: str = Field(default="", description="Name of the media file")
    file_size: int = Field(default=0, description="Size of the media file in bytes")
    video: FFProbeVideoTrack = Field(
        default_factory=FFProbeVideoTrack, description="Video track metadata"
    )
    duration: float = Field(
        default=0.0,
        description="Duration of the video in seconds",
    )
    format: list[str] = Field(default=[], description="Format of the video")
    bitrate: int = Field(
        default=0, description="Bitrate of the video in bits per second"
    )
    audio: list[FFProbeAudioTrack] = Field(
        default=[], description="Audio tracks in the video"
    )
    subtitles: list[FFProbeSubtitleTrack] = Field(
        default=[], description="Subtitles in the video"
    )

    @property
    def size_in_mb(self) -> float:
        """Return the file size in MB, rounded to 2 decimal places"""
        return round(self.file_size / (1024 * 1024), 2)

    @property
    def duration_in_mins(self) -> float:
        """Return the duration in minutes, rounded to 2 decimal places"""
        return round(self.duration / 60, 2)


class FFProbeTagsMixin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class Tags(BaseModel):
        model_config = ConfigDict(extra="ignore")
        language: str | None = None

    tags: Tags | None = Field(default=None)


class FFProbeBaseStream(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    codec_name: str | None = ""
    r_frame_rate: str | None = "0/0"

    @property
    def fps(self) -> float:
        """Calculate frames per second from `r_frame_rate`"""
        if not self.r_frame_rate:
            return 0.0
        try:
            return float(Fraction(self.r_frame_rate))
        except (ZeroDivisionError, ValueError, Exception):
            return 0.0


class FFProbeDataStream(FFProbeBaseStream, FFProbeTagsMixin):
    codec_type: Literal["data"] = "data"


class FFProbeVideoStream(FFProbeBaseStream):
    codec_type: Literal["video"] = "video"
    width: int = 0
    height: int = 0


class FFProbeAudioStream(FFProbeBaseStream, FFProbeTagsMixin):
    codec_type: Literal["audio"] = "audio"
    channels: int = 0
    sample_rate: int | str = 0

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _parse_sample_rate(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0


class FFProbeSubtitleStream(FFProbeBaseStream, FFProbeTagsMixin):
    codec_type: Literal["subtitle"] = "subtitle"


class FFProbeAttachmentStream(FFProbeBaseStream, FFProbeTagsMixin):
    codec_type: Literal["attachment"] = "attachment"


class FFProbeOtherStream(FFProbeBaseStream):
    codec_type: Literal["other"] = "other"



class FFProbeFormat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str = ""
    format_name: str | None = ""
    duration: float | str = 0.0
    size: int | str = 0
    bit_rate: int | str = 0

    @field_validator("duration", "size", "bit_rate", mode="before")
    @classmethod
    def _parse_numeric(cls, v: Any) -> float | int:
        if v is None:
            return 0
        try:
            return float(v) if isinstance(v, (str, float)) else int(v)
        except (ValueError, TypeError):
            return 0


class FFProbeResponse(BaseModel):
    """Model representing the ffprobe response"""

    model_config = ConfigDict(extra="ignore")

    # Retain nested aliases for backward compatibility with external references
    TagsMixin: ClassVar[Any] = FFProbeTagsMixin
    BaseStream: ClassVar[Any] = FFProbeBaseStream
    DataStream: ClassVar[Any] = FFProbeDataStream
    VideoStream: ClassVar[Any] = FFProbeVideoStream
    AudioStream: ClassVar[Any] = FFProbeAudioStream
    SubtitleStream: ClassVar[Any] = FFProbeSubtitleStream
    AttachmentStream: ClassVar[Any] = FFProbeAttachmentStream
    OtherStream: ClassVar[Any] = FFProbeOtherStream
    Format: ClassVar[Any] = FFProbeFormat

    streams: list[Any] = Field(default_factory=list)

    format: FFProbeFormat


def extract_filename_from_url(download_url: str) -> str:
    """
    Extract a UTF-8 decoded filename from a URL.

    This will:
    - Take the last path segment
    - URL-decode percent-encoded characters (e.g. `%20` -> space)
    - Assume UTF-8, which is the standard for URL encoding
    """
    from urllib.parse import unquote, urlparse

    if not (path := urlparse(download_url).path):
        return ""

    raw_name = path.rsplit("/", 1)[-1]

    return unquote(raw_name, encoding="utf-8", errors="replace")


def parse_media_url(url: str) -> FFProbeMediaMetadata | None:
    """
    Parse a media file using ffprobe and return its metadata.

    Args:
        url: URL of the media file

    Returns:
        MediaMetadata object

    Raises:
        FileNotFoundError: If the file doesn't exist
        subprocess.CalledProcessError: If ffprobe returns an error
        ValueError: If an unexpected error occurs while parsing the file
    """

    if not url:
        raise ValueError("No download URL provided")

    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-analyzeduration",
            "2M",
            "-probesize",
            "10M",
            "-print_format",
            "json=compact=1",
            "-show_entries",
            (
                "format=filename,size,duration,bit_rate,format_name:"
                "stream=index,codec_name,codec_type,width,height,"
                "r_frame_rate,channels,sample_rate,bit_rate:"
                "stream_tags=language,title"
            ),
            "-i",
            url,
        ]

        try:
            result = subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,  # keep bytes for orjson
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""

            raise RuntimeError(f"ffprobe error while probing {url}: {stderr}") from exc
        except Exception as exc:
            raise ValueError(
                f"Unexpected error invoking ffprobe for {url}: {exc}"
            ) from exc

        try:
            raw_probe_data = orjson.loads(result.stdout)
            probe_data = FFProbeResponse(**raw_probe_data)
        except Exception as exc:
            raise ValueError(
                f"Failed to parse ffprobe JSON output for {url}: {exc}"
            ) from exc

        if not probe_data:
            raise ValueError(f"ffprobe returned no data for {url}")

        format_info = probe_data.format

        metadata = FFProbeMediaMetadata(
            filename=extract_filename_from_url(url),
            file_size=int(format_info.size),
            duration=round(float(format_info.duration), 2),
            format=(
                (format_info.format_name or "unknown").split(",")
                if format_info.format_name
                else []
            ),
            bitrate=int(format_info.bit_rate),
        )

        for stream in probe_data.streams:
            match stream:
                case FFProbeVideoStream():
                    if not metadata.video.codec:
                        # Apparently there's multiple video codecs..
                        # the first one should always be correct though.
                        metadata.video = FFProbeVideoTrack(
                            codec=stream.codec_name or "",
                            width=stream.width,
                            height=stream.height,
                            frame_rate=round(stream.fps, 2),
                        )
                case FFProbeAudioStream():
                    metadata.audio.append(
                        FFProbeAudioTrack(
                            codec=stream.codec_name or "",
                            channels=stream.channels,
                            sample_rate=int(stream.sample_rate)
                            if isinstance(stream.sample_rate, (int, float))
                            else 0,
                            language=stream.tags.language
                            if stream.tags and stream.tags.language
                            else "",
                        )
                    )
                case FFProbeSubtitleStream():
                    metadata.subtitles.append(
                        FFProbeSubtitleTrack(
                            codec=stream.codec_name or "",
                            language=stream.tags.language
                            if stream.tags and stream.tags.language
                            else "",
                        )
                    )
                case FFProbeDataStream() | FFProbeAttachmentStream() | FFProbeOtherStream():
                    pass
                case _:
                    pass

        return metadata
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe error: {e}")
    except Exception as e:
        raise ValueError(f"Unexpected error during ffprobe of {url}: {e}")


