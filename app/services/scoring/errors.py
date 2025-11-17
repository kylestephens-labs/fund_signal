"""Shared error classes for the scoring engine and repositories."""

from __future__ import annotations


class ScoringEngineError(RuntimeError):
    """Base exception raised by the ChatGPT scoring engine."""

    def __init__(self, message: str, code: str = "SCORING_ENGINE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ScoringProviderError(ScoringEngineError):
    """Raised when an upstream AI provider fails."""


class ScoringValidationError(ScoringEngineError):
    """Raised when a model response cannot be parsed safely."""


class ScorePersistenceError(ScoringEngineError):
    """Raised when the repository fails to save or retrieve scores."""
