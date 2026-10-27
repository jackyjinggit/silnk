from typing import Literal, Optional

import base64

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import db
from jwt import auth_device_id, verify_device_or_merchant

router = APIRouter()

# 现场图上限 2MB：X5 抓的是 JPEG，一张 1280x720 大约 100KB，2MB 足够留余量。
MAX_PHOTO_BYTES = 2 * 1024 * 1024


def _decode_photo(data):
    """把 base64 现场图还原成字节；格式或大小不合法时返回 None。"""
    if not data:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > MAX_PHOTO_BYTES:
        return None
    if raw[:2] != b"\xff\xd8":
        return None
    return raw


class EjectEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    event: Literal["sheet_out"]
    source: Literal["manual", "vision"]
    ts: int = Field(gt=0)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    photo: Optional[str] = Field(default=None, max_length=3100000)


@router.get("/api/edge/{device_id}/activity")
def edge_activity(device_id: str, scope: str = Depends(verify_device_or_merchant)):
    token_device = auth_device_id(scope)
    if token_device and token_device != device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)
    if db.get_device(device_id) is None:
        return JSONResponse({"message": "device not found"}, status_code=404)
    return {"message": "success", "data": db.get_edge_activity(device_id)}


@router.post("/api/edge/{device_id}/eject")
def eject_event(device_id: str, request: EjectEvent,
                scope: str = Depends(verify_device_or_merchant)):
    token_device = auth_device_id(scope)
    if token_device and token_device != device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)
    if db.get_device(device_id) is None:
        return JSONResponse({"message": "device not found"}, status_code=404)
    if not db.get_edge_activity(device_id)["active"]:
        return JSONResponse({"message": "no active order"}, status_code=409)
    activity = db.get_edge_activity(device_id)
    event = db.add_eject_event(device_id, request.dict(exclude={"photo"}),
                               order_id=activity.get("order_id"))
    if event is None:
        return JSONResponse({"message": "event_id already used with different data"},
                            status_code=409)
    photo = _decode_photo(request.photo)
    if photo is not None:
        db.save_eject_photo(device_id, request.event_id, photo)
        event["has_photo"] = True
    return {"message": "success", "data": {"event": event}}


@router.get("/api/edge/{device_id}/eject/{event_id}/photo")
def eject_photo(device_id: str, event_id: str,
                scope: str = Depends(verify_device_or_merchant)):
    token_device = auth_device_id(scope)
    if token_device and token_device != device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)
    if db.get_device(device_id) is None:
        return JSONResponse({"message": "device not found"}, status_code=404)
    image, mime = db.get_eject_photo(device_id, event_id)
    if image is None:
        return JSONResponse({"message": "photo not found"}, status_code=404)
    return Response(content=image, media_type=mime or "image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/edge/{device_id}/eject")
def eject_event_list(device_id: str, limit: int = Query(default=100, ge=1, le=500),
                     since: Optional[int] = Query(default=None, gt=0),
                     scope: str = Depends(verify_device_or_merchant)):
    token_device = auth_device_id(scope)
    if token_device and token_device != device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)
    if db.get_device(device_id) is None:
        return JSONResponse({"message": "device not found"}, status_code=404)
    events = db.list_eject_events(device_id, limit=limit, since=since)
    return {"message": "success", "data": {"events": events}}
