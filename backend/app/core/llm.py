"""LLM 工厂组件。

Note:
    本模块的构造只兼容 OpenAI 接口协议, 所有的模型供应商配置的时候应该选择 OpenAI 接口形式的 base_url。
"""

import asyncio
from functools import lru_cache
from typing import Any

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from app.config import LLMSettings, get_llmsettings

PING_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
"""模型供应商接口探活请求分档超时。"""


def _is_deepseek_flavor(settings: LLMSettings) -> bool:
    """判断模型配置是否为 deepseek。

    Args:
        settings: 模型配置。

    Returns:
        bool: 显式声明为 deepseek, 或 base_url 含 deepseek 关键字时为 True。
    """
    return settings.llm_provider == "deepseek" or "deepseek" in (settings.llm_base_url or "").lower()


def _is_ollama_flavor(settings: LLMSettings) -> bool:
    """判断模型配置是否为 ollama。

    Args:
        settings: 模型配置。

    Returns:
        bool: 显式声明为 ollama, 或 base_url 含 ollama 关键字时为 True。
    """
    return settings.llm_provider == "ollama" or "ollama" in (settings.llm_base_url or "").lower()


def create_llm(**overrides) -> BaseChatModel:
    """以默认设置创建大模型, 可以自行修改部分参数。

    Args:
        **overrides: 需要覆盖的参数。

    Returns:
        BaseChatModel: 创建出来的大模型实例。

    Raises:
        ValueError: overrides 参数中出现了未定义的key。
    """
    settings: LLMSettings = get_llmsettings()  # 获取大模型默认设置
    # 参数字典
    params: dict[str, Any] = {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "timeout": settings.llm_timeout,
        "max_retries": settings.llm_max_retries,
        "extra_body": None,  # 提供商特有参数
    }
    # 检查参数格式
    unknown = set(overrides) - set(params)
    if unknown:
        raise ValueError(f"未知的参数{overrides}")
    # 覆盖参数
    params.update(overrides)

    # 展开参数字典
    kwargs: dict = dict(
        model=params["model"],
        model_provider="openai",
        base_url=params["base_url"],
        api_key=params["api_key"],
        temperature=params["temperature"],
        max_tokens=params["max_tokens"],
        timeout=params["timeout"],
        max_retries=params["max_retries"],
        extra_body=params["extra_body"],
        stream_usage=True,
    )

    # 创建模型实例
    return init_chat_model(**kwargs)


@lru_cache
def get_chat_model() -> BaseChatModel:
    """获得大模型实例。"""
    return create_llm()


@lru_cache
def create_planner_llm() -> BaseChatModel:
    """创建计划器llm。

    Note:
        本函数是针对deepseek思考模式无法支持结构化输出的策略。
    """
    settings = get_llmsettings()
    # 如果是 deepseek 则关闭思考模式
    if _is_deepseek_flavor(settings):
        return create_llm(extra_body={"thinking": {"type": "disabled"}})
    return create_llm()


def get_planner_structured_method() -> str:
    """规划器结构化输出方式, ollama 走 json 模式。"""
    settings = get_llmsettings()
    if _is_ollama_flavor(settings):
        return "json_mode"
    return "function_calling"


async def ping_provider() -> tuple[bool, str]:
    """供应商接口可达性检查。

    Returns:
        tuple[bool, str]: (是否可达, 详细信息)。

    Note:
        本函数只用于验证模型供应商接口是否连通和鉴权测试, 不验证模型生成功能是否正常。
        测试原理为发送 GET {base_url}/models 请求。
    """
    settings = get_llmsettings()
    url = f"{settings.llm_base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT) as client:
            res = await client.get(url, headers={"Authorization": f"Bearer {settings.llm_api_key}"})
        res.raise_for_status()
    except httpx.HTTPStatusError as e:
        # 通常代表模型提供商没有提供此接口
        if e.response.status_code == 404:
            return False, f"模型提供商未提供模型列表接口: {url}"
        return False, f"模型供应商返回错误码: {e.response.status_code}"
    except httpx.HTTPError:
        return False, "无法连接至供应商"
    return True, "供应商可达"


async def ping_chat_model(llm: BaseChatModel | None = None, timeout: float = 15.0) -> tuple[bool, str]:
    """模型连通性检查。

    Args:
        llm: 大模型实例, 为空时默认使用全局实例。
        timeout: 超时时间, 默认为 15.0 秒。

    Returns:
        tuple[bool, str]: (是否连通, 详细信息)。

    Note:
        本函数会发起一次真实模型调用, 会产生一定的费用。
    """
    llm = llm or get_chat_model()
    try:
        # 向大模型发送消息测试连通性
        res = await asyncio.wait_for(llm.bind(max_tokens=1).ainvoke("ping"), timeout=timeout)
        return True, f"Connected. LLM return: {res.content}"
    except Exception as e:
        return False, f"Disconnected. Details: \n{e}"
