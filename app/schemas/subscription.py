"""订阅接口的请求 / 响应模型"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SubscriptionStatusResponse(BaseModel):
    """订阅状态 + 免费额度快照。"""

    is_subscribed: bool
    product_id: str | None = None
    platform: str | None = None
    expires_at: datetime | None = None
    auto_renewing: bool = False
    voice_note_count: int = 0
    voice_note_limit: int | None = None
    can_create_voice_note: bool = True
    image_note_count: int = 0
    image_note_limit: int | None = None
    can_create_image_note: bool = True
    file_note_count: int = 0
    file_note_limit: int | None = None
    can_create_file_note: bool = True
    # 单条录音最长时长（秒），按档位给：免费 600（10 分钟），订阅 14400（4 小时）。
    # 时长不再按月累计，所以没有「本月已用/剩余」这类字段。
    voice_note_max_seconds: int


class VerifySubscriptionRequest(BaseModel):
    """客户端购买成功后上报的凭证。

    到期时间、是否续订等权益字段一律由商店返回，不接受客户端上报。
    旧版客户端多传的字段会被忽略。
    """

    platform: Literal["android", "ios"]
    product_id: str = Field(min_length=1, max_length=100)
    purchase_token: str | None = Field(default=None, max_length=1000)
    transaction_id: str | None = Field(default=None, max_length=255)
