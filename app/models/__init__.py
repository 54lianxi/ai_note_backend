# 导入所有 ORM 模型，确保 SQLAlchemy metadata 能发现所有表
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.record import Record

__all__ = ["User", "Conversation", "Message", "Record"]
