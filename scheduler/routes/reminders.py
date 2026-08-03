"""
Reminder routes — /reminders CRUD
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from routes.auth import auth_user

router = APIRouter(tags=["reminders"])


@router.get("/reminders", summary="查询提醒列表")
def list_reminders(
    user: dict = Depends(auth_user),
    status: Optional[str] = Query(default=None),
):
    from tools import execute_tool
    return execute_tool("list_reminders", {"user_id": user["id"], "status": status})


@router.get("/reminders/{reminder_id}", summary="查看单个提醒")
def get_reminder(reminder_id: str, user: dict = Depends(auth_user)):
    from tools import execute_tool
    result = execute_tool("get_reminder", {"reminder_id": reminder_id, "user_id": user["id"]})
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.delete("/reminders/{reminder_id}", summary="取消提醒")
def cancel_reminder(reminder_id: str, user: dict = Depends(auth_user)):
    from tools import execute_tool
    result = execute_tool("cancel_reminder", {"reminder_id": reminder_id, "user_id": user["id"]})
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["message"])
    return result
