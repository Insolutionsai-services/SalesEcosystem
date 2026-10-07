"""The durable work queue: follow-ups, meetings, approvals, hand-offs, reorder nudges."""
from datetime import datetime

from sqlalchemy import select, update

from ..core.models import Task
from ..core.utils import now


def add_task(s, tenant_id: int, contact_id: int | None, kind: str, due_at: datetime | None = None, **data) -> Task:
    task = Task(tenant_id=tenant_id, contact_id=contact_id, kind=kind, due_at=due_at or now(), data=data)
    s.add(task)
    s.flush()
    return task


def has_open_task(s, contact_id: int, kind: str) -> bool:
    return s.scalars(select(Task.id).where(Task.contact_id == contact_id, Task.kind == kind,
                                           Task.status == "open")).first() is not None


def close_open_tasks(s, contact_id: int, kind: str, status: str = "done") -> None:
    s.execute(update(Task).where(Task.contact_id == contact_id, Task.kind == kind, Task.status == "open")
              .values(status=status))


def due_tasks(s, tenant_id: int, kinds, at: datetime) -> list[Task]:
    return s.scalars(select(Task).where(Task.tenant_id == tenant_id, Task.status == "open", Task.due_at <= at,
                                        Task.kind.in_(kinds))).all()


def open_contact_ids(s, tenant_id: int, kind: str) -> set[int]:
    return set(s.scalars(select(Task.contact_id).where(Task.tenant_id == tenant_id, Task.kind == kind,
                                                       Task.status == "open")))


def list_tasks(s, tenant_id: int, status: str = "open", kind: str | None = None, limit: int = 500) -> list[Task]:
    q = select(Task).where(Task.tenant_id == tenant_id, Task.status == status).order_by(Task.due_at)
    if kind:
        q = q.where(Task.kind == kind)
    return s.scalars(q.limit(limit)).all()
