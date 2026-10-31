"""生成/更新后端静态 UI 语言包。

en.json 是基准，zh.json 由 front/tool/generate_l10n.py 从 ARB 生成。
其他语言放在 app/static/language_packs/：只要这个文件存在，接口就直接返回它，
不会再走运行时翻译。所以这里写出的一定要是完整语言包，不能只写新增的几个键。

用法：
    .venv/bin/python scripts/generate_language_packs.py ja
    .venv/bin/python scripts/generate_language_packs.py ja --refresh voiceExport,voiceExportFailed
    .venv/bin/python scripts/generate_language_packs.py es fr de pt it ko ru ar hi id tr vi th

只翻目标语言缺失的键；--refresh 指定的键即使已存在也重新翻译。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.app_info import SUPPORTED_LANGUAGES
from app.services.llm_service import LLMService

_PACK_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "language_packs"
# en 是翻译基准，zh 由 ARB 生成，都不走 LLM。
_GENERATED = {"en", "zh"}


async def _translate(
    service: LLMService,
    source: dict[str, str],
    language: str,
) -> dict[str, str]:
    """一次翻一整包；输出被 token 上限截断时对半拆开重试。"""
    try:
        return await service.translate_language_pack(source, language)
    except json.JSONDecodeError:
        if len(source) <= 20:
            raise
        keys = list(source)
        mid = len(keys) // 2
        print(f"  {language}: 输出被截断，拆成 {mid} + {len(keys) - mid} 个键重试")
        result = await _translate(
            service, {key: source[key] for key in keys[:mid]}, language
        )
        result.update(
            await _translate(
                service, {key: source[key] for key in keys[mid:]}, language
            )
        )
        return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成/更新静态 UI 语言包")
    parser.add_argument(
        "languages",
        nargs="*",
        help="语言代码，留空则处理已有静态文件的所有非 en/zh 语言",
    )
    parser.add_argument(
        "--refresh",
        default="",
        help="逗号分隔的键名，即使已存在也重新翻译",
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计要翻译多少键")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    en = json.loads((_PACK_DIR / "en.json").read_text("utf-8"))
    refresh = {key.strip() for key in args.refresh.split(",") if key.strip()}
    unknown = refresh - set(en)
    if unknown:
        print(f"en.json 里没有这些键：{', '.join(sorted(unknown))}")
        return 1

    if args.languages:
        languages = [code.lower() for code in args.languages]
    else:
        languages = sorted(
            path.stem
            for path in _PACK_DIR.glob("*.json")
            if path.stem not in _GENERATED
        )
    invalid = [
        code
        for code in languages
        if code not in SUPPORTED_LANGUAGES or code in _GENERATED
    ]
    if invalid:
        print(f"不支持生成的语言：{', '.join(invalid)}")
        return 1

    service = LLMService()
    failed: list[str] = []
    for code in languages:
        path = _PACK_DIR / f"{code}.json"
        pack = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        todo = {key: en[key] for key in en if key not in pack or key in refresh}
        obsolete = set(pack) - set(en)
        print(f"{code}: 共 {len(en)} 键，待翻译 {len(todo)}，删除过期 {len(obsolete)}")
        if args.dry_run:
            continue
        try:
            translated = await _translate(service, todo, code) if todo else {}
        except Exception as error:  # noqa: BLE001
            print(f"  {code}: 失败 {error}")
            failed.append(code)
            continue

        merged = {
            key: translated.get(key, pack.get(key))
            for key in en
            if key in translated or key in pack
        }
        path.write_text(
            json.dumps(merged, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        for key in sorted(refresh & set(todo)):
            print(f"  {key} = {merged[key]}")

    if failed:
        print(f"失败语言：{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
