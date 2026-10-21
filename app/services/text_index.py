"""长文本分块。索引和检索使用同一套边界，保证 chunk_index 能定位回原文。"""

CHUNK_CHARS = 1800
CHUNK_OVERLAP = 180
EMBED_BATCH_SIZE = 32


def split_text_chunks(
    text: str,
    chunk_chars: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    normalized = text.replace("\x00", "").replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if len(normalized) <= chunk_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_chars, len(normalized))
        if end < len(normalized):
            search_start = start + chunk_chars // 2
            boundary = max(
                normalized.rfind("\n", search_start, end),
                normalized.rfind("。", search_start, end),
                normalized.rfind("；", search_start, end),
                normalized.rfind(" ", search_start, end),
            )
            if boundary >= search_start:
                end = boundary + 1
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)
    return chunks
