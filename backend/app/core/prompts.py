"""集中管理提示词"""

from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# 主模型提示词
SYSTEM_PROMPT = ChatPromptTemplate.from_messages(
    [("system", "你是一个AI智能助手, 可以使用工具完成用户的任务。回答使用{language}。")]
)


def get_system_messages(language: str = "中文") -> list[BaseMessage]:
    """填充主模型提示词并返回"""
    return SYSTEM_PROMPT.format_messages(language=language)


# 计划器提示词
PLANNER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是一个任务规划器, 将用户的需求拆分成多个条例清晰、有先后顺序的todo计划。"
            "要求步骤之间不能有重叠, 设计的步骤数不宜过多, 也不可为了追求步骤少而放弃了清晰的条理。"
            "如果用户的需求足够简单, 无需分步即可直接回答, 可以返回空的todos列表。",
        ),
        MessagesPlaceholder(variable_name="existing_plan_context"),  # 消息占位符
        ("human", "{task}"),
    ]
)

PLANNER_EXISTING_PLAN_PROMPT = ChatPromptTemplate.from_template(
    "当前已有计划: \n{existing_plan}"
    "\n如果用户的新消息是对该计划的延续(如要求继续等), 返回空的列表以沿用旧计划列表。"
    "\n如果是全新的任务, 请给出新的计划列表。当然如果任务过于简单不需要设置计划, 也可以返回空列表。"
)

PLAN_EXECUTION_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "当前正在执行计划: \n{plan_lines}"
            "\n如果用户当前的问题与计划相关, 严格按计划完成任务。"
            "\n如果用户提问的问题是与计划无关的问题, 则直接回答问题, 不需要执行以下规定的 mark 工作。"
            "\n开始执行某个计划前必须调用 mark_todo_start 工具。"
            "\n完成一个计划后必须调用 mark_todo_done 工具。"
            "\n在给出最终回复之前, 必须确保所有已开始的计划步骤都已调用 mark_todo_done 工具。",
        ),
    ]
)


# 工具执行反馈文案
TOOL_FEEDBACK_REJECTED = "用户拒绝此操作"
TOOL_FEEDBACK_TODO_NOT_FOUND = "未找到id为{todo_id}的计划。核对ID后重试"
TOOL_FEEDBACK_EXEC_FAILED = "工具执行失败: {error}"
