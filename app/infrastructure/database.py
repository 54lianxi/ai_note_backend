"""PostgreSQL 数据库连接管理"""
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.core.security import DEV_USER_ID

# 创建异步引擎
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    # 统一用 UTC 会话时区，时间一律以 UTC 存储，绝不依赖服务器本地时区。
    connect_args={"server_settings": {"timezone": "UTC"}},
)

# 创建异步 Session 工厂
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类"""


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入：获取数据库 Session"""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """初始化数据库（创建所有表 + 种子默认用户）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 兼容已经存在的 records 表。项目暂时没有独立迁移目录，
        # 这里让新增的编辑和附件字段在旧数据库上也能直接使用。
        await conn.execute(text(
            "ALTER TABLE records ADD COLUMN IF NOT EXISTS file_url VARCHAR(500)"
        ))
        await conn.execute(text(
            "ALTER TABLE records ADD COLUMN IF NOT EXISTS file_name VARCHAR(255)"
        ))
        await conn.execute(text(
            "ALTER TABLE records ADD COLUMN IF NOT EXISTS "
            "file_content_type VARCHAR(120)"
        ))
        await conn.execute(text(
            "ALTER TABLE records ADD COLUMN IF NOT EXISTS file_size INTEGER"
        ))
        await conn.execute(text(
            "ALTER TABLE records ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"
        ))
        await conn.execute(text(
            "UPDATE records SET updated_at = created_at WHERE updated_at IS NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS "
            "related_record_ids JSONB"
        ))
        await conn.execute(text(
            "UPDATE messages SET related_record_ids = '[]'::jsonb "
            "WHERE related_record_ids IS NULL"
        ))
        # 统一时间存储：历史列是 TIMESTAMP（无时区），按 UTC 解释后转成 TIMESTAMPTZ。
        # 只处理当前仍为 naive 的列，避免重复转换造成时区偏移。
        legacy_columns = (await conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND data_type = 'timestamp without time zone' "
            "AND table_name IN ('records', 'conversations', 'messages', 'users') "
            "AND column_name IN ('created_at', 'updated_at')"
        ))).all()
        for table_name, column_name in legacy_columns:
            await conn.execute(text(
                f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" '
                f"TYPE TIMESTAMPTZ USING \"{column_name}\" AT TIME ZONE 'UTC'"
            ))
        await conn.execute(text(
            "ALTER TABLE records ALTER COLUMN updated_at SET DEFAULT now()"
        ))

    # 确保默认开发用户存在
    from app.models.user import User
    default_user_id = uuid.UUID(DEV_USER_ID)
    async with async_session_factory() as session:
        result = await session.execute(select(1).where(User.id == default_user_id))
        if result.scalar() is None:
            session.add(User(
                id=default_user_id,
                username="default",
                hashed_password="dev-only",
            ))
            await session.commit()


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()
