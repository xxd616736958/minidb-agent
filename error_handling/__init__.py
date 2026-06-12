"""Structured error handling and self-repair helpers."""

from error_handling.classifier import ErrorClassifier
from error_handling.recovery import RecoveryEngine

__all__ = ["ErrorClassifier", "RecoveryEngine"]
