from typing import TypeVar

from program.core.runner import Runner
from program.settings.models import Observable

T = TypeVar("T", bound=Observable, default=Observable)


class AnalysisService(Runner[T, None, bool]):
    """Base class for all analysis services"""
