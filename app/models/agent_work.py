from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import END_SESSION_SCOPE_TASK, END_SESSION_SCOPES


class AgentWorkReportTarget(StrEnum):
    SESSION = "session"
    PARENT_WORK = "parent_work"


class AgentWorkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: str = Field(min_length=1)
    execution_scope: str = END_SESSION_SCOPE_TASK
    report_target: AgentWorkReportTarget = AgentWorkReportTarget.SESSION
    parent_work_id: int | None = Field(default=None, ge=1)
    subagent_id: str | None = Field(default=None, min_length=1, max_length=32)
    parent_tool_call_id: str | None = Field(default=None, min_length=1, max_length=100)
    instruction_message_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_execution_contract(self) -> "AgentWorkSpec":
        if self.execution_scope not in END_SESSION_SCOPES:
            raise ValueError("unsupported execution scope")
        if self.report_target == AgentWorkReportTarget.PARENT_WORK:
            if self.parent_work_id is None:
                raise ValueError("parent_work_id is required for parent_work report target")
            if self.subagent_id is None:
                raise ValueError("subagent_id is required for parent_work report target")
            if self.parent_tool_call_id is None:
                raise ValueError("parent_tool_call_id is required for parent_work report target")
            if self.execution_scope != END_SESSION_SCOPE_TASK:
                raise ValueError("parent_work report target requires end_session scope task")
        elif any(value is not None for value in (self.parent_work_id, self.subagent_id, self.parent_tool_call_id)):
            raise ValueError("parent fields are only valid for parent_work report target")
        return self
