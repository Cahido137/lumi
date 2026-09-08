"""健康检查接口的响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CheckItem(BaseModel):
    """单项依赖检查结果。"""

    ok: bool = Field(..., description="该项检查是否通过")
    detail: str = Field("", description="补充说明细节")
    latency_ms: float | None = Field(None, description="本项检查消耗时间, 单位毫秒")

    @classmethod
    def passed(cls, detail: str = "", latency_ms: float | None = None) -> CheckItem:
        """构造一个通过检查结果。"""
        return cls(ok=True, detail=detail, latency_ms=latency_ms)

    @classmethod
    def failed(cls, detail: str = "", latency_ms: float | None = None) -> CheckItem:
        """构造一个未通过检查结果。"""
        return cls(ok=False, detail=detail, latency_ms=latency_ms)


class LivenessResponse(BaseModel):
    """存活探针响应体。"""

    status: str = Field("ok", description="存活状态")


class ReadinessChecks(BaseModel):
    """就绪探针的逐项检查结果。"""

    database: CheckItem = Field(..., description="数据库可达性检查结果")


class ReadinessResponse(BaseModel):
    """就绪探针响应体。

    Note:
        不就绪时端点返回 HTTP 503, 不返回本响应体。
    """

    status: str = Field("ready", description="就绪状态")
    checks: ReadinessChecks = Field(..., description="各依赖的检查结果")


class DeepChecks(BaseModel):
    """深度检查。

    Note:
        本项检查包括数据库可达性检查、LangGraph 检查点可达性检查、模型提供商接口连通性检查、模型生成链路检查。
    """

    database: CheckItem = Field(..., description="数据库可达性检查")
    checkpoint: CheckItem = Field(..., description="LangGraph 检查点可达性检查")
    provider: CheckItem = Field(..., description="模型提供商接口连通性检查")
    llm_generation: CheckItem = Field(..., description="模型生成链路检查")


class DeepCheckResponse(BaseModel):
    """深度检查响应体。"""

    status: Literal["healthy", "degraded"] = Field(..., description="检查结果")
    checks: DeepChecks = Field(..., description="逐项检查结果详情")
