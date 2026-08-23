from uuid import uuid4
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func, BigInteger, Identity, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


def gen_uuid() -> str:
    """生成字符串UUID"""
    return str(uuid4())

class User(Base):
    """用户信息表"""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="用户唯一标识ID")
    uid: Mapped[int] = mapped_column(BigInteger, Identity(start=10000), unique=True, index=True, comment="用户UID")
    username: Mapped[str] = mapped_column(String(20), unique=True, comment="用户名(唯一)")
    nickname: Mapped[str | None] = mapped_column(String(50), comment="昵称")
    password_hash: Mapped[str] = mapped_column(String(200), comment="密码哈希")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="用户注册时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="用户信息更新时间")


class Session(Base):
    """会话表, 一次对话为一条会话记录"""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="会话唯一标识ID")
    user_id: Mapped[str] = mapped_column(ForeignKey(User.id, ondelete="CASCADE"), index=True, comment="会话所属用户ID")
    title: Mapped[str] = mapped_column(String(200), default="新会话", comment="会话标题")
    status: Mapped[str] = mapped_column(String(20), default="active", comment="active=进行中, archived=已归档")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")


class Message(Base):
    """消息表, 记录每一轮对话"""
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="消息唯一标识ID")
    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID")
    role: Mapped[str] = mapped_column(String(20), comment="user=用户, assistant=模型, system=系统提示词, tool=工具消息")
    content: Mapped[str] = mapped_column(Text, default="", comment="消息正文")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"), comment="消息产生时间")


class Todo(Base):
    """计划表"""
    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="任务唯一标识ID")
    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID")
    title: Mapped[str] = mapped_column(String(500), comment="任务描述")
    status: Mapped[str] = mapped_column(String(20), default="pending", comment="pending=待执行, in_progress=执行中, done=已完成, failed=执行失败")
    position: Mapped[int] = mapped_column(Integer, default=0, comment="任务在计划中的序号")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="任务创建时间")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="状态变更时间")


class ToolExecution(Base):
    """工具执行表"""
    __tablename__ = "tool_executions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="工具唯一标识ID")
    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID")
    tool_name: Mapped[str] = mapped_column(String(100), comment="工具名称")
    tool_input: Mapped[dict] = mapped_column(JSONB, default=dict, comment="工具入参")
    tool_output: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具输出")
    status: Mapped[str] = mapped_column(String(20), default="success", comment="success=成功, error=失败, pending=等待审批, rejected=已拒绝")
    needs_approval: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否需要审批")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="开始执行时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="结束执行时间")


class Approval(Base):
    """审批表"""
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=gen_uuid, comment="审批唯一标识ID")
    session_id: Mapped[str] = mapped_column(ForeignKey(Session.id, ondelete="CASCADE"), index=True, comment="所属会话ID")
    thread_id: Mapped[str] = mapped_column(String(200), comment="线程ID, 用于中断恢复")
    tool_execution_id: Mapped[str] = mapped_column(ForeignKey(ToolExecution.id, ondelete="CASCADE"), index=True, comment="关联工具执行ID")
    status: Mapped[str] = mapped_column(String(20), default="pending", comment="pending=待审批, approved=已批准, rejected=已拒绝")
    scope: Mapped[str] = mapped_column(String(20), default="one_time", comment="one_time=批准这一次, command=始终允许此工具执行此命令, tool=始终允许此工具")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="审批时间")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), comment="审批创建时间")