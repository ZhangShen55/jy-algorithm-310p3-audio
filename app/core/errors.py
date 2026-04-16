from __future__ import annotations


class ASRProcessingError(RuntimeError):
    def __init__(self, message: str, *, details: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
