import pytest

from app.core.i18n import (
    DEFAULT_LANGUAGE,
    normalize_language,
    search_answer_prompts,
    t,
    vision_system_prompt,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_LANGUAGE),
        ("", DEFAULT_LANGUAGE),
        ("en", "en"),
        ("en-US,en;q=0.9,zh;q=0.8", "en"),
        ("zh-CN,zh;q=0.9", "zh"),
        ("EN-gb", "en"),
        # 不支持的语言回落到默认语言
        ("ja-JP,ja;q=0.9", DEFAULT_LANGUAGE),
        ("fr", DEFAULT_LANGUAGE),
    ],
)
def test_normalize_language(raw, expected):
    assert normalize_language(raw) == expected


def test_t_uses_requested_language():
    assert t("en", "record_not_found") == "Record not found"
    assert t("zh", "record_not_found") == "记录不存在"


def test_t_falls_back_to_default_language():
    assert t("ja", "record_not_found") == t(DEFAULT_LANGUAGE, "record_not_found")


def test_t_formats_placeholders():
    assert "boom" in t("en", "search_failed", error="boom")
    assert "boom" in t("zh", "search_failed", error="boom")


def test_search_answer_prompts_follow_language():
    results = [{"content": "notes", "summary": "sum", "score": 0.9}]

    en_system, en_prompt = search_answer_prompts("en", "question", results)
    zh_system, zh_prompt = search_answer_prompts("zh", "question", results)

    assert "Answer in English" in en_system
    assert "用中文回答" in zh_system
    assert "User query: question" in en_prompt
    assert "用户查询: question" in zh_prompt
    assert "notes" in en_prompt and "notes" in zh_prompt


def test_vision_prompt_follows_language():
    assert "English" in vision_system_prompt("en")
    assert "中文" in vision_system_prompt("zh")
