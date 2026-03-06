class RateLimitError(Exception):
    """Raised when a service responds with an HTTP 429 Too Many Requests status code."""

    def __init__(
        self, message: str = "Rate limit exceeded", retry_after: float | None = None
    ):
        self.message = message
        self.retry_after = retry_after
        super().__init__(self.message)
