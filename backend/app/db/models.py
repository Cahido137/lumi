"""业务数据表的 ORM 模型类定义。

本模块定义了所有数据库业务表, 涵盖用户、会话、消息、计划、工具执行记录、审批单等。
图运行过程中的中间状态与中断断点所用到的数据表由 LangGraph 框架统一管理, 本模块不负责。

Note:
    所有业务数据表的主键统一为字符串形式的 UUID, 使用 uuid4, 由 gen_uuid 函数生成。
    时间戳统一由数据库侧的 server_default 填充。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Integer, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def gen_uuid() -> str:
    """生成字符串形式的 UUID, 用作主键的生成。

    Returns:
        str: 一个新的 UUID4 字符串。

    Note:
        仅在 SQLAlchemy 的列默认值中传入本函数, 以函数本身传入而不是函数执行结果。
    """
    return str(uuid4())


class User(Base):
    """用户表, 存储用户账号凭据与相关个人信息。

    id 为内部主键, 不应暴露给用户, 只应在服务器查询中使用。
    uid 为对外编号, 可暴露给用户。
    username 为登录用户名, 全局唯一。
    nickname 为昵称, 不唯一。
    password_hash 存储密码哈希值, 不存储密码明文。

    Note:
        令牌中携带的主体标识是 uid 而非 id, 使用 uid 查询用户。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="用户唯一标识ID")
    uid: Mapped[int] = mapped_column(BigInteger, Identity(start=10000), unique=True, index=True, comment="用户UID")
    username: Mapped[str] = mapped_column(String(20), unique=True, comment="用户名(唯一)")
    nickname: Mapped[str | None] = mapped_column(String(50), comment="昵称")
    password_hash: Mapped[str] = mapped_column(String(200), comment="密码哈希")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="用户注册时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="用户信息更新时间"
    )


class Session(Base):
    """会话表, 一次对话为一条会话记录, 同时承载该会话的上下文压缩状态。

    user_id 作为外键, 关联用户表中的用户, 级联删除。
    status 区分进行中与已归档。
    summary_text 与 summary_until_message_id 是耦合字段, 同时为空或同时有值。
    summary_text 保存最近一次压缩生成的摘要, summary_until_message_id 保存该摘要所覆盖到的最后一条消息ID,
    重建历史消息时以 summary_until_message_id 作为边界进行重建。
    has_pending_task 用于标记上一轮运行被人工打断且计划尚未执行完毕。
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="会话唯一标识ID")
    user_id: Mapped[str] = mapped_column(ForeignKey(User.id, ondelete="CASCADE"), index=True, comment="会话所属用户ID")
    title: Mapped[str] = mapped_column(String(200), default="新会话", comment="会话标题")
    status: Mapped[str] = mapped_column(String(20), default="active", comment="active=进行中, archived=已归档")
    has_pending_task: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否存在被打断未完成的任务")
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="上下文摘要, None表示未压缩")
    summary_until_message_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="摘要覆盖的最后一条消息ID"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )


class Message(Base):
    """消息表, 按消息生成顺序记录会话中的每一条消息。

    role 记录消息的所属角色。
    当类型为 assistant 时, 应使用 tool_calls 保存模型声明的工具调用列表。
    当类型为 tool 时, 应使用 tool_name 与 tool_call_id, 对应工具发起的那次调用。
    所有消息都应填充 content 字段, 无正文时填充空字符串。
    usage 用于保存模型返回的 token 用量元数据。

    Note:
        created_at 使用 clock_timestamp 保存语句执行时刻, 以保证记录的时间顺序正确。
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="消息唯一标识ID")
    session_id: Mapped[str] = mapped_column(
        ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID"
    )
    role: Mapped[str] = mapped_column(String(20), comment="user=用户, assistant=模型, system=系统提示词, tool=工具消息")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息正文")
    tool_call_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="ToolMessage存储的tool_call_id"
    )
    usage: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="模型返回的usage_metadata元数据"
    )  # {input_tokens, output_tokens, total_tokens}
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="ToolMessage的工具名")
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True, comment="模型声明的工具调用列表")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), comment="消息产生时间"
    )


class Todo(Base):
    """计划表, 用于保存任务步骤。

    session_id 作为外键, 关联计划表所属的会话, 级联删除。
    position 记录本步骤在步骤列表中的位置, 用作排序。
    status 记录本步骤的执行状态。
    """

    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="任务唯一标识ID")
    session_id: Mapped[str] = mapped_column(
        ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID"
    )
    title: Mapped[str] = mapped_column(String(500), comment="任务描述")
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="pending=待执行, in_progress=执行中, done=已完成, failed=执行失败"
    )
    position: Mapped[int] = mapped_column(Integer, default=0, comment="任务在计划中的序号")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="任务创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="状态变更时间"
    )


class ToolExecution(Base):
    """工具执行表, 记录每一次工具调用的信息。

    tool_call_id 记录发起本次工具调用的工具调用标识。
    needs_approval 用于标记本工具是否需要经过审批。
    status 用于标记工具执行状态。

    Note:
        待审批工具会在执行前就创建状态为 pending 的记录。
    """

    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="工具唯一标识ID")
    session_id: Mapped[str] = mapped_column(
        ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID"
    )
    tool_name: Mapped[str] = mapped_column(String(100), comment="工具名称")
    tool_call_id: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="发起该工具调用的tool_call_id")
    tool_input: Mapped[dict] = mapped_column(JSONB, default=dict, comment="工具入参")
    tool_output: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具输出")
    status: Mapped[str] = mapped_column(
        String(20), default="success", comment="success=成功, error=失败, pending=等待审批, rejected=已拒绝"
    )
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否需要审批")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="开始执行时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="结束执行时间")


class Approval(Base):
    """审批表, 记录需要审批工具的人工审批单。

    tool_execution_id 作为外键, 关联被审批的工具执行记录, 级联删除。
    status 表示审批单自身的状态。
    scope 表示本次审批的授权范围。

    Note:
        thread_id 保存发起本次中断的 LangGraph 检查点线程标识。
        同一轮运行可能产生多张审批单, 它们使用相同的 thread_id。
    """

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="审批唯一标识ID")
    session_id: Mapped[str] = mapped_column(
        ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID"
    )
    thread_id: Mapped[str] = mapped_column(String(200), comment="线程ID, 用于中断恢复")
    tool_execution_id: Mapped[str] = mapped_column(
        ForeignKey(ToolExecution.id, ondelete="CASCADE"), index=True, comment="关联工具执行ID"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="pending=待审批, approved=已批准, rejected=已拒绝"
    )
    scope: Mapped[str] = mapped_column(
        String(20),
        default="one_time",
        comment="one_time=批准这一次, command=始终允许此工具执行此命令, tool=始终允许此工具",
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="审批时间")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="审批创建时间"
    )
