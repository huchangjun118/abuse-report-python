"""领域异常。"""


class AbuseError(Exception):
    """基础异常。"""


class PermissionDenied(AbuseError):
    """越权操作。"""


class InvalidTransition(AbuseError):
    """非法状态转换。"""


class NotFound(AbuseError):
    """对象不存在。"""


class RateLimited(AbuseError):
    """触发限速。"""


class Conflict(AbuseError):
    """并发冲突，需重试。"""
