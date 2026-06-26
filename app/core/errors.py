from __future__ import annotations


class ASRProcessingError(RuntimeError):
    def __init__(self, message: str, *, details: str | dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ConcurrencyLimitError(ASRProcessingError):
    """Raised when the maximum number of concurrent requests is exceeded."""
    pass


class AudioMetricsProcessingError(RuntimeError):
    def __init__(self, message: str, *, details: str | dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
