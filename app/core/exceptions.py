"""核心工具 - 统一异常处理"""
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class AppException(HTTPException):
    """应用自定义异常"""
    
    def __init__(self, status_code: int = 500, detail: str = "服务器内部错误"):
        super().__init__(status_code=status_code, detail=detail)


class NotFoundException(AppException):
    """资源不存在"""
    def __init__(self, detail: str = "资源不存在"):
        super().__init__(status_code=404, detail=detail)


class BadRequestException(AppException):
    """请求参数错误"""
    def __init__(self, detail: str = "请求参数错误"):
        super().__init__(status_code=400, detail=detail)


class UnauthorizedException(AppException):
    """未授权"""
    def __init__(self, detail: str = "未授权"):
        super().__init__(status_code=401, detail=detail)


async def app_exception_handler(request: Request, exc: AppException):
    """统一异常处理器"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail},
    )
