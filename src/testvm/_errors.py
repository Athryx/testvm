class TestvmError(Exception):
    """Base exception for user-facing failures."""


class UnsupportedArchitectureError(TestvmError):
    """Raised when an architecture is unknown or unsupported."""


class CommandExecutionError(TestvmError):
    """Raised when an external command fails."""
