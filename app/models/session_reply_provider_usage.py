# 历史兼容文件：仅为已发布迁移脚本提供旧表结构定义。
# 被 scripts/migration_20260825_add_session_reply_provider_usage.py 引用。
# 不参与现行业务读写或应用自动建表。
# 清理上述历史迁移脚本时，须一并删除本文件。

from sqlalchemy import Column, DateTime, Enum, ForeignKeyConstraint, Integer, MetaData, String, Table, UniqueConstraint

metadata = MetaData()

session_reply_work_item = Table(
    "session_reply_work_item",
    metadata,
    Column("id", Integer),
    Column("session_id", String(100)),
    Column("uid", String(100)),
)

session_reply_provider_usage = Table(
    "session_reply_provider_usage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True, index=True),
    Column("provider_request_id", String(64), nullable=False, index=True),
    Column("work_id", Integer, nullable=False, index=True),
    Column("session_id", String(100), nullable=False, index=True),
    Column("uid", String(100), nullable=False, index=True),
    Column("input_tokens", Integer, nullable=False),
    Column("cached_tokens", Integer, nullable=False),
    Column(
        "request_purpose",
        Enum(
            "MAIN_DIALOGUE",
            "BACKGROUND_SUMMARY",
            "SCHEDULED_SUMMARY",
            "AUDIT_AUXILIARY",
            "CONFIRMED_AUXILIARY",
            "LEGACY_UNKNOWN",
            name="sessionreplyproviderrequestpurpose",
        ),
        nullable=False,
        index=True,
    ),
    Column("output_tokens", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), index=True),
    UniqueConstraint("provider_request_id", name="uq_session_reply_provider_usage_request"),
    ForeignKeyConstraint(
        ["work_id", "session_id", "uid"],
        [
            "session_reply_work_item.id",
            "session_reply_work_item.session_id",
            "session_reply_work_item.uid",
        ],
        name="fk_session_reply_provider_usage_work_owner",
        ondelete="CASCADE",
    ),
)


class SessionReplyProviderUsage:
    """Historical migration table definition only; not an ORM model."""

    __table__ = session_reply_provider_usage
