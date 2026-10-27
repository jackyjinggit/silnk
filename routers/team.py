"""团队协作看板 API（独立应用 /team 使用）。

鉴权沿用管理端 SECRET_KEY（Authorization: Bearer <SECRET_KEY>）。
字段：任务 / 团队(模块) / 状态 / 优先级 / 进度 / 指派人 / 标签 / 关注 / 最后更新。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

import db
from jwt import security, secret_key_matches
from models import ReturnResult

router = APIRouter()


def verify_secret_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """校验看板访问密钥（与管理端同一个 SECRET_KEY，常量时间比较）。"""
    if secret_key_matches(credentials.credentials):
        return True
    raise HTTPException(status_code=401, detail="Invalid secret key")


class TaskCreate(BaseModel):
    """新建任务请求体。"""

    title: str
    team: str = "用户端"
    status: str = "未开始"
    priority: str = "P2"
    progress: int = 0
    assignee: str = ""
    tags: str = ""
    note: str = ""
    starred: bool = False


class TaskPatch(BaseModel):
    """局部更新任务请求体：只传要改的字段。"""

    title: Optional[str] = None
    team: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    progress: Optional[int] = None
    assignee: Optional[str] = None
    tags: Optional[str] = None
    note: Optional[str] = None
    starred: Optional[bool] = None


def _validate(team=None, status=None, priority=None):
    """校验枚举值，返回错误消息或 None。"""
    if team is not None and team not in db.TASK_TEAMS:
        return f"team must be one of {list(db.TASK_TEAMS)}"
    if status is not None and status not in db.TASK_STATUSES:
        return f"status must be one of {list(db.TASK_STATUSES)}"
    if priority is not None and priority not in db.TASK_PRIORITIES:
        return f"priority must be one of {list(db.TASK_PRIORITIES)}"
    return None


def _meta():
    return {
        "teams": list(db.TASK_TEAMS),
        "statuses": list(db.TASK_STATUSES),
        "priorities": list(db.TASK_PRIORITIES),
        # 成员与分工，前端「负责人」一栏直接用来显示角色
        "members": dict(db.TASK_MEMBERS),
    }


@router.get("/api/team/tasks")
def team_tasks(bool=Depends(verify_secret_key)):
    """全部任务（看板一次拉全量，筛选与统计在前端做）。"""
    return ReturnResult(200, "success", {"tasks": db.list_tasks(), "meta": _meta()})


@router.post("/api/team/tasks")
def team_task_create(request: TaskCreate, bool=Depends(verify_secret_key)):
    """新建任务。"""
    title = (request.title or "").strip()
    if not title:
        return ReturnResult(200, "title is required", {})
    error = _validate(request.team, request.status, request.priority)
    if error:
        return ReturnResult(200, error, {})
    task = db.add_task(
        title, team=request.team, status=request.status,
        priority=request.priority, progress=request.progress,
        assignee=request.assignee.strip(), tags=request.tags.strip(),
        note=request.note.strip(), starred=request.starred,
    )
    return ReturnResult(200, "success", {"task": task})


@router.post("/api/team/tasks/{task_id}")
def team_task_update(task_id: int, request: TaskPatch, bool=Depends(verify_secret_key)):
    """局部更新任务（状态、优先级、进度、指派人等）。"""
    error = _validate(request.team, request.status, request.priority)
    if error:
        return ReturnResult(200, error, {})
    if request.title is not None and not request.title.strip():
        return ReturnResult(200, "title can not be empty", {})
    fields = request.dict(exclude_unset=True)
    if "title" in fields:
        fields["title"] = fields["title"].strip()
    for key in ("assignee", "tags", "note"):
        if key in fields and fields[key] is not None:
            fields[key] = fields[key].strip()
    task = db.update_task(task_id, **fields)
    if task is None:
        return ReturnResult(200, "task not found", {})
    return ReturnResult(200, "success", {"task": task})


@router.delete("/api/team/tasks/{task_id}")
def team_task_delete(task_id: int, bool=Depends(verify_secret_key)):
    """删除任务。"""
    removed = db.delete_task(task_id)
    return ReturnResult(200, "success" if removed else "task not found",
                        {"task_id": task_id})
