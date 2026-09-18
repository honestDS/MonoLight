from dataclasses import dataclass

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan


@dataclass(frozen=True, slots=True)
class SessionTodoWriteResult:
    session_exists: bool
    plan: SessionTodoPlan | None
    written: bool


class CRUDSessionTodoPlan:
    async def get_by_session_id(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
    ) -> SessionTodoPlan | None:
        result = await db.execute(
            select(SessionTodoPlan)
            .where(
                SessionTodoPlan.uid == uid,
                SessionTodoPlan.session_id == session_id,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def read(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
    ) -> tuple[bool, SessionTodoPlan | None]:
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.uid == uid,
                ChatSession.session_id == session_id,
            )
        )
        if session_result.scalars().first() is None:
            return False, None
        return True, await self.get_by_session_id(db, uid=uid, session_id=session_id)

    async def delete_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        conditions = [SessionTodoPlan.session_id == session_id]
        if not is_admin:
            conditions.append(SessionTodoPlan.uid == uid)
        result = await db.execute(delete(SessionTodoPlan).where(*conditions).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def write(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        todos: list[dict[str, str]],
        expected_revision: int,
        replace_existing: bool = False,
        commit: bool = True,
    ) -> SessionTodoWriteResult:
        await db.execute(
            update(ChatSession)
            .where(
                ChatSession.uid == uid,
                ChatSession.session_id == session_id,
            )
            .values(session_id=ChatSession.session_id)
            .execution_options(synchronize_session=False)
        )
        session_result = await db.execute(
            select(ChatSession)
            .where(
                ChatSession.uid == uid,
                ChatSession.session_id == session_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if session_result.scalars().first() is None:
            return SessionTodoWriteResult(session_exists=False, plan=None, written=False)

        plan_result = await db.execute(
            select(SessionTodoPlan)
            .where(
                SessionTodoPlan.uid == uid,
                SessionTodoPlan.session_id == session_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        plan = plan_result.scalars().first()
        current_revision = 0 if plan is None else plan.revision
        if expected_revision != current_revision:
            if commit:
                await db.commit()
            return SessionTodoWriteResult(session_exists=True, plan=plan, written=False)

        if plan is not None and not replace_existing:
            current_items = {item.get("content", "").strip(): item.get("status") for item in plan.todos}
            incoming_items = {item.get("content", "").strip(): item.get("status") for item in todos}
            has_unfinished = any(status != "completed" for status in current_items.values())
            removes_existing_item = has_unfinished and not set(current_items).issubset(incoming_items)
            regresses_completed_item = has_unfinished and any(status == "completed" and incoming_items.get(content) != "completed" for content, status in current_items.items())
            if removes_existing_item or regresses_completed_item:
                if commit:
                    await db.commit()
                return SessionTodoWriteResult(session_exists=True, plan=plan, written=False)

        now = get_local_time()
        if plan is None:
            plan = SessionTodoPlan(
                uid=uid,
                session_id=session_id,
                todos=[dict(item) for item in todos],
                revision=1,
                created_at=now,
                updated_at=now,
            )
        else:
            plan.todos = [dict(item) for item in todos]
            plan.revision += 1
            plan.updated_at = now

        db.add(plan)
        if commit:
            await db.commit()
        else:
            await db.flush()
        return SessionTodoWriteResult(session_exists=True, plan=plan, written=True)


session_todo_crud = CRUDSessionTodoPlan()
