"""时间工具：后端统一以 UTC 存储和传输，不依赖服务器本地时区。"""
from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    """把时间统一成带时区的 UTC；无时区的值按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_utc(value: str | None) -> datetime | None:
    """解析 ISO 时间字符串；不带时区的历史字符串按 UTC 解释。"""
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))
