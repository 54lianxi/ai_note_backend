from app.services.text_index import split_text_chunks


def test_short_text_returns_single_chunk():
    assert split_text_chunks("短文档正文") == ["短文档正文"]


def test_long_text_keeps_overlap_and_covers_the_end():
    text = "".join(f"第{i}段内容。" for i in range(1000))

    chunks = split_text_chunks(text, chunk_chars=200, overlap=20)

    assert len(chunks) > 1
    assert "第0段内容。" in chunks[0]
    assert "第999段内容。" in chunks[-1]
    assert chunks[0][-20:] in chunks[1]
