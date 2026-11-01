"""MinIO 对象 key 生成规则的单测：key 一律 records/UUID.ext，客户端文件名不进 key。"""
import re

from app.infrastructure.minio_client import MinIOClient


def _client() -> MinIOClient:
    # 跳过 __init__（避免真实网络连接），_make_object_name 只依赖纯逻辑。
    return MinIOClient.__new__(MinIOClient)


def test_object_name_is_uuid_key_without_client_filename():
    client = _client()
    name = client._make_object_name("voice 1.m4a", "audio/mp4")
    # 客户端文件名（含空格）不进 key，key 统一 records/UUID.ext
    assert re.fullmatch(r"records/[0-9a-f]{8}-[0-9a-f-]{27}\.m4a", name)


def test_object_name_extension_fallbacks():
    client = _client()
    # 没有文件名：按 MIME 推断
    assert client._make_object_name(None, "image/jpeg").endswith(".jpg")
    # 文件名没有扩展名：回落 MIME
    assert client._make_object_name("archive", "text/markdown").endswith(".bin")
    # 临时文件后缀兜底（语音走 upload_file_from_path 的路径）
    assert client._make_object_name(None, "audio/mp4", fallback_ext=".m4a").endswith(".m4a")
    # 文件名扩展名优先于 MIME
    assert client._make_object_name("a.WAV", "audio/mpeg").endswith(".WAV")
