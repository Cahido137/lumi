"""LLM 工厂组件"""

from functools import lru_cache

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from app.config import get_llmsettings, LLMSettings


PROVIDER_TUPLE = (
    ("deepseek", "deepseek"),
    ("bigmodel", "zhipu"),
    ("dashscope", "qwen"),
    ("moonshot", "kimi"),
    ("ollama", "ollama"),
    ("anthropic", "anthropic")
)

OPENAI_COMPATIBLE = {"deepseek", "zhipu", "qwen", "kimi", "ollama"}

def detect_provider(base_url: str) -> str:
    """根据 base_url 猜测供应商"""
    url = (base_url or "").lower()

    # 根据 url 中是否含有关键字查找
    for keyword, provider in PROVIDER_TUPLE:
        if keyword in url:
            return provider
    # 默认识别为 openai
    return "openai"

def _resolve_provider(provider: str) -> str:
    """将一部分兼容openai的国产厂商视作openai"""
    if provider in OPENAI_COMPATIBLE:
        return "openai"
    return provider


def create_llm(**overrides) -> BaseChatModel:
    """
    以默认设置创建大模型, 可以自行修改部分参数

    Args:
        overrides: 修改的参数

    Returns:
        创建出来的大模型实例
    """
    settings: LLMSettings = get_llmsettings()  # 获取大模型默认设置
    # 参数字典
    params = {
        "provider": settings.llm_provider or detect_provider(settings.llm_base_url),
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "timeout": settings.llm_timeout,
        "max_retries": settings.llm_max_retries,
        "extra_body": None  # 提供商特有参数
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
        model_provider=_resolve_provider(params["provider"]),
        base_url=params["base_url"],
        api_key=params["api_key"],
        temperature=params["temperature"],
        max_tokens=params["max_tokens"],
        timeout=params["timeout"],
        max_retries=params["max_retries"],
        extra_body=params["extra_body"]
    )

    # 创建模型实例
    return init_chat_model(**kwargs)


@lru_cache
def get_chat_model() -> BaseChatModel:
    """获得大模型实例"""
    return create_llm()

@lru_cache
def create_planner_llm() -> BaseChatModel:
    """
    创建计划器llm
    针对deepseek思考模式无法支持结构化输出的策略
    """
    settings = get_llmsettings()
    provider = settings.llm_provider or detect_provider(settings.llm_base_url)
    is_deepseek = (provider == "deepseek" or "deepseek" in settings.llm_base_url.lower())
    # 如果是 deepseek 则关闭思考模式
    if is_deepseek:
        return create_llm(extra_body={"thinking": {"type": "disabled"}})
    return create_llm()


def get_planner_structured_method() -> str:
    """规划器结构化输出方式, ollama 走 json 模式"""
    settings = get_llmsettings()
    provider = settings.llm_provider or detect_provider(settings.llm_base_url)
    if provider == "ollama" or "ollama" in (settings.llm_base_url or "").lower():
        return "json_mode"
    return "function_calling"


async def ping_chat_model(llm: BaseChatModel | None = None, timeout: float = 15.0) -> tuple[bool, str]:
    """
    模型连通性检查

    Args:
        llm: 大模型实例
        timeout: 超时时间

    Returns:
        (是否联通, 详细信息)
    """
    import asyncio
    llm = llm or get_chat_model()
    try:
        # 向大模型发送消息测试连通性
        res = await asyncio.wait_for(llm.ainvoke("ping"), timeout=timeout)
        return True, f"Connected. LLM return: {res.content}"
    except Exception as e:
        return False, f"Disconnected. Details: \n{e}"