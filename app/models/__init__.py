# 导入所有 ORM 模型，确保 SQLAlchemy metadata 能发现所有表
from app.models.conversation import Conversation, Message
from app.models.record import Record
from app.models.report import AIReport
from app.models.subscription import Subscription
from app.models.user import User

__all__ = [
    "AIReport",
    "Conversation",
    "Message",
    "Record",
    "Subscription",
    "User",
]
