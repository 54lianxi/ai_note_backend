"""MinIO 对象存储客户端"""
import io
import json
from uuid import uuid4
from minio import Minio
from minio.error import S3Error

from app.config import settings


class MinIOClient:
    """MinIO 客户端封装"""
    
    def __init__(self):
        self.client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        self.bucket = settings.MINIO_BUCKET
        self._ensure_bucket()
    
    def _ensure_bucket(self):
        """确保 Bucket 存在，并允许匿名只读（客户端要能直接播放/预览对象）"""
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            self._ensure_public_read()
        except S3Error as e:
            raise RuntimeError(f"MinIO bucket 初始化失败: {e}")

    def _ensure_public_read(self):
        """给 Bucket 设置只读公开策略。

        对象 URL 会被前端直接用于 <img>/音频播放，不带签名，所以必须公开可读，
        否则匿名 GET 会返回 403 AccessDenied。
        """
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{self.bucket}/*"],
                }
            ],
        }
        self.client.set_bucket_policy(self.bucket, json.dumps(policy))
    
    def upload_file(self, file_data: bytes, content_type: str, filename: str | None = None) -> str:
        """
        上传文件到 MinIO
        :param file_data: 文件字节数据
        :param content_type: MIME 类型
        :param filename: 自定义文件名（可选）
        :return: 文件访问 URL
        """
        if filename is None:
            ext = self._get_extension(content_type)
            filename = f"{uuid4()}{ext}"
        
        object_name = f"records/{filename}"
        
        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(file_data),
            length=len(file_data),
            content_type=content_type,
        )
        
        return self.get_url(object_name)
    
    def get_url(self, object_name: str) -> str:
        """获取对象访问 URL"""
        scheme = "https" if settings.MINIO_SECURE else "http"
        return f"{scheme}://{settings.MINIO_ENDPOINT}/{self.bucket}/{object_name}"
    
    def delete_file(self, object_name: str) -> None:
        """删除文件"""
        self.client.remove_object(self.bucket, object_name)
    
    @staticmethod
    def _get_extension(content_type: str) -> str:
        """根据 MIME 类型获取文件扩展名"""
        mapping = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
        }
        return mapping.get(content_type, ".bin")


# 全局客户端实例（延迟初始化）
_minio_client: MinIOClient | None = None


def get_minio_client() -> MinIOClient:
    """获取 MinIO 客户端单例"""
    global _minio_client
    if _minio_client is None:
        _minio_client = MinIOClient()
    return _minio_client
