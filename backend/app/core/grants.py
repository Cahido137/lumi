"""工具授权"""

from collections.abc import Callable

from pydantic import BaseModel, Field

from app.utils.dict import normalize_dict

# 各工具的授权身份键
GRANT_KEY_EXTRACTORS: dict[str, Callable[[dict], dict]] = {
    "run_shell": lambda args: {"command": args.get("command")},
    "write_file": lambda args: {"path": args.get("path")},
}


def extract_grant_key(tool_name: str, args: dict | None) -> str:
    """提取工具入参中用于授权比对的身份键"""
    extractor = GRANT_KEY_EXTRACTORS.get(tool_name)
    payload = None
    if extractor is not None:
        payload = extractor(args or {})
    else:
        payload = args or {}
    return normalize_dict(payload)


class Grants(BaseModel):
    """工具授权领域类"""

    tool: list[str] = Field(default_factory=list, description="已整体授权的工具名列表")
    command: dict[str, list[str]] = Field(default_factory=dict, description="已授权工具命令")

    def is_granted(self, tool_name: str, args: dict | None) -> bool:
        """判断工具或命令是否已授权"""
        if tool_name in self.tool:  # 如果工具已整体授权直接通过
            return True
        commands = self.command.get(tool_name, [])  # 获取该工具已授权的命令列表
        return extract_grant_key(tool_name, args) in commands
