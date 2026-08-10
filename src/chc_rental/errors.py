"""Domain errors for the allowlist and profile repositories."""


class NotAllowlistedError(PermissionError):
    """Raised when a Telegram user ID that is not on the owner-controlled allowlist
    attempts to access or modify profile data."""


class ProfileAccessDeniedError(PermissionError):
    """Raised when an allowlisted user attempts to access another user's profile."""


class ProfileNotFoundError(LookupError):
    """Raised when a requested profile ID does not exist."""


class DuplicateProfileNameError(ValueError):
    """Raised when a user already has a profile with the given name."""


class DeliveryRetryLimitExceededError(RuntimeError):
    """Raised when a caller tries to retry a delivery that already exhausted
    its bounded retry attempts."""


class BudgetExhaustedError(RuntimeError):
    """Raised when a caller tries to consume more than the remaining daily
    budget, or when the circuit breaker has already tripped for the date."""
