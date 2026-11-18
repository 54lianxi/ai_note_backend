"""每日新闻定时任务：抓取当天热点新闻，LLM 总结成中文后存为目标用户的笔记。

默认数据源为 Hacker News（无鉴权、服务器可直连），目标用户默认为
审核账号，可通过 NEWS_TARGET_USER_ID 覆盖。
"""

import asyncio
import logging
import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from zoneinfo import ZoneInfo

from app.config import settings
from app.core.http import build_async_client
from app.services.llm_service import LLMService
from app.workflows.record_workflow import execute_record_workflow

logger = logging.getLogger(__name__)

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"

# 与 auth.py 中审核账号保持同一套 UUID 生成规则，保证是同一个用户。
REVIEW_USER_NAMESPACE = uuid.UUID("8f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
REVIEW_USER_ID = str(uuid.uuid5(REVIEW_USER_NAMESPACE, "review_account"))

_MAX_STORY_CANDIDATES = 5


async def fetch_top_story() -> dict | None:
    """取 Hacker News 当前热榜第一条有效新闻，全部无效时返回 None。"""
    async with build_async_client() as client:
        resp = await client.get(f"{HN_API_BASE}/topstories.json")
        resp.raise_for_status()
        story_ids = resp.json() or []
        for story_id in story_ids[:_MAX_STORY_CANDIDATES]:
            detail = (
                (
                    await client.get(f"{HN_API_BASE}/item/{story_id}.json")
                ).json()
            )
            if detail and not detail.get("dead") and not detail.get("deleted"):
                return detail
    return None


def _build_note_content(story: dict, summary: str) -> str:
    title = story.get("title", "").strip()
    url = story.get("url") or f"https://news.ycombinator.com/item?id={story.get('id')}"
    lines = [f"📰 {title}"]
    if summary.strip():
        lines.append("")
        lines.append(summary.strip())
    lines.append("")
    lines.append(f"原文链接：{url}")
    return "\n".join(lines)


def _send_email(subject: str, body: str) -> bool:
    """把新闻内容发到配置的邮箱；未配置 SMTP 凭据时记日志并跳过。"""
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning("邮件未发送：SMTP_USER / SMTP_PASSWORD 未配置")
        return False
    to_addr = settings.NEWS_EMAIL_TO or settings.SMTP_USER

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = settings.SMTP_USER
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=30
    ) as server:
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.SMTP_USER, [to_addr], msg.as_string())
    logger.info("每日新闻：邮件已发送至 %s", to_addr)
    return True


async def create_news_record() -> str | None:
    """抓一条新闻并写入目标用户的笔记，返回记录 ID；无新闻时返回 None。"""
    story = await fetch_top_story()
    if story is None:
        logger.warning("每日新闻：未获取到有效新闻，本次跳过")
        return None

    title = story.get("title", "")
    text = story.get("text") or ""
    summary = ""
    try:
        prompt = (
            f"用中文 3-5 条要点总结下面这条新闻；若正文为空，"
            f"就根据标题合理推断并简要介绍背景。只输出要点：\n\n标题：{title}"
        )
        if text:
            prompt += f"\n\n正文：{text[:2000]}"
        summary = await LLMService().generate(prompt)
    except Exception:
        logger.exception("每日新闻：LLM 总结失败，降级为只记标题和链接")

    content = _build_note_content(story, summary)
    result = await execute_record_workflow(
        user_id=settings.NEWS_TARGET_USER_ID or REVIEW_USER_ID,
        content_type="text",
        content=content,
        language="zh",
    )
    logger.info("每日新闻：已创建笔记 %s（%s）", result.record_id, title)

    try:
        await asyncio.to_thread(
            _send_email, f"每日新闻｜{title}", content
        )
    except Exception:
        logger.exception("每日新闻：邮件发送失败（笔记已保存，不影响记录）")
    return result.record_id


def _seconds_until_next_run() -> float:
    tz = ZoneInfo(settings.NEWS_TZ)
    now = datetime.now(tz)
    next_run = now.replace(hour=settings.NEWS_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


async def run_daily_news_scheduler() -> None:
    """常驻循环：每天 NEWS_HOUR（NEWS_TZ 时区）执行一次抓取。"""
    logger.info(
        "每日新闻调度启动：每天 %02d:00（%s）执行", settings.NEWS_HOUR, settings.NEWS_TZ
    )
    while True:
        await asyncio.sleep(_seconds_until_next_run())
        try:
            await create_news_record()
        except Exception:
            logger.exception("每日新闻：执行失败，等待下一天重试")
