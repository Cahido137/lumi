"""工具授权"""

from pydantic import BaseModel, Field

from app.utils.dict import normalize_dict


class Grants(BaseModel):
    """工具授权领域类"""
    tool: list[str] = Field(default_factory=list, description="已整体授权的工具名列表")
    command: dict[str, list[str]] = Field(default_factory=dict, description="已授权工具命令")

    def is_granted(self, tool_name: str, args: dict | None) -> bool:
        """判断工具或命令是否已授权"""
        if tool_name in self.tool:  # 如果工具已整体授权直接通过
            return True
        commands = self.command.get(tool_name, [])  # 获取该工具已授权的命令列表
        return normalize_dict(args) in commands