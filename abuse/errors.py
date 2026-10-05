"""Domain and infrastructure errors."""


class AbuseError(Exception):
    """Base error."""


class NotFoundError(AbuseError):
    pass


class DuplicateError(AbuseError):
    pass


class InvalidStateTransitionError(AbuseError):
    pass


class PermissionDeniedError(AbuseError):
    pass


class RateLimitedError(AbuseError):
    pass


class ValidationError(AbuseError):
    pass


class ConflictError(AbuseError):
    pass


class ConflictRetryError(AbuseError):
    """Optimistic concurrency conflict; the operation may be retried."""
