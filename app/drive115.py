"""115 驱动。流程只依赖 Drive115 这几个方法，方便替换/测试。

- OpenDrive：115 开放平台（扫码授权，access_token 自动续期），负责建目录、列目录、移动、删除、磁力离线。
- P115Drive：Cookie + p115client。没有 AppID 时它负责全部操作；有开放平台授权时只用来转存分享链接
  （开放平台没有这个接口）。
- CompositeDrive：按配置选择后端：已授权开放平台 → 走 Open；否则有 Cookie → 走 Cookie；都没有就报错。

注意：开发环境无法联网，以上接口都是按公开文档写的，尚未用真实账号联调；请先调用 /api/selftest 验证。
"""
import asyncio
import base64
import hashlib
import re
import secrets
import time
from typing import Protocol

import httpx

from . import settings
from .config import cfg


class Drive115(Protocol):
    async def ensure_dir(self, parent: int, name: str) -> int: ...
    async def mkdir(self, parent: int, name: str) -> int: ...
    async def receive_share(self, share_code: str, receive_code: str, dest: int) -> None: ...
    async def add_offline(self, magnet: str, dest: int) -> str: ...
    async def offline_state(self, info_hash: str) -> str: ...  # running | done | failed
    async def list_files(self, cid: int) -> list[dict]: ...  # 递归，仅文件 [{id,name,size}]
    async def move(self, ids: list, dest: int) -> None: ...
    async def delete(self, ids: list) -> None: ...


def _check(r: dict):
    if isinstance(r, dict) and r.get("state") is False:
        raise RuntimeError(f"115 接口失败: {r}")
    return r


def _ids(ids):
    return {f"fid[{i}]": x for i, x in enumerate(ids)}


class P115Drive:
    def __init__(self, cookie: str):
        from p115client import P115Client
        self.c = P115Client(cookie)

    async def _run(self, fn, *a, **kw):
        return _check(await asyncio.to_thread(fn, *a, **kw))

    async def _children(self, cid: int) -> list[dict]:
        r = await self._run(self.c.fs_files, {"cid": cid, "limit": 1000, "show_dir": 1})
        return r.get("data", [])

    async def list_dirs(self, cid: int) -> list[dict]:
        return [{"id": int(it["cid"]), "name": it.get("n", "")} for it in await self._children(cid) if "fid" not in it]

    async def mkdir(self, parent: int, name: str) -> int:
        r = await self._run(self.c.fs_mkdir, {"cname": name, "pid": parent})
        return int(r.get("cid") or r["data"]["file_id"])

    async def ensure_dir(self, parent: int, name: str) -> int:
        for it in await self._children(parent):
            if "fid" not in it and it.get("n") == name:
                return int(it["cid"])
        return await self.mkdir(parent, name)

    async def receive_share(self, share_code, receive_code, dest):
        snap = await self._run(self.c.share_snap, {"share_code": share_code, "receive_code": receive_code,
                                                   "cid": 0, "limit": 1000})
        items = snap.get("data", {}).get("list", [])
        ids = [str(it.get("fid") or it.get("cid")) for it in items]
        if not ids:
            raise RuntimeError("分享为空或已失效")
        await self._run(self.c.share_receive, {"share_code": share_code, "receive_code": receive_code,
                                               "file_id": ",".join(ids), "cid": dest})

    async def add_offline(self, magnet: str, dest: int) -> str:
        r = await self._run(self.c.offline_add_url, {"url": magnet, "wp_path_id": dest})
        h = r.get("info_hash") or (re.search(r"btih:([0-9a-fA-F]{40})", magnet) or [None, ""])[1]
        return h.lower()

    async def offline_state(self, info_hash: str) -> str:
        r = await self._run(self.c.offline_list, {"page": 1})
        for t in r.get("tasks", []):
            if str(t.get("info_hash", "")).lower() == info_hash:
                if t.get("status") == 2 or t.get("percentDone") == 100:
                    return "done"
                return "failed" if t.get("status") == -1 else "running"
        return "running"

    async def list_files(self, cid: int) -> list[dict]:
        out = []
        for it in await self._children(cid):
            if "fid" in it:
                out.append({"id": it["fid"], "name": it["n"], "size": int(it.get("s", 0))})
            else:
                out += await self.list_files(int(it["cid"]))
        return out

    async def move(self, ids, dest):
        await self._run(self.c.fs_move, {"pid": dest, **_ids(ids)})

    async def delete(self, ids):
        await self._run(self.c.fs_delete, _ids(ids))


# ---------------------------------------------------------------- 开放平台

API = "https://proapi.115.com"
AUTH = "https://passportapi.115.com"
QR_STATUS = "https://qrcodeapi.115.com/get/status/"
_pending: dict = {}  # 进行中的扫码授权


def _ok(d) -> bool:
    return bool(d.get("state"))


async def auth_start(app_id: str) -> dict:
    """第一步：取设备码和二维码内容（PKCE）。"""
    if not app_id:
        raise RuntimeError("请先填写并保存 115 开放平台 AppID")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(AUTH + "/open/authDeviceCode", data={
            "client_id": app_id, "code_challenge": challenge, "code_challenge_method": "sha256"})
    d = r.json()
    if not _ok(d):
        raise RuntimeError(f"获取二维码失败：{d.get('message') or d.get('error') or d}")
    _pending.clear()
    _pending.update(d["data"], verifier=verifier)
    return {"qrcode": d["data"]["qrcode"]}


async def auth_poll() -> dict:
    """第二步：轮询扫码状态（服务端长轮询），确认后换取令牌并保存。"""
    if not _pending:
        raise RuntimeError("没有进行中的授权，请重新点「授权 115」")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(QR_STATUS, params={k: _pending[k] for k in ("uid", "time", "sign")})
        st = (r.json().get("data") or {}).get("status", 0)
    except httpx.TimeoutException:
        return {"status": 0, "done": False}
    if st is not None and st < 0:
        _pending.clear()
        raise RuntimeError("二维码已过期或已取消，请重新授权")
    if st != 2:
        return {"status": st, "done": False}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(AUTH + "/open/deviceCodeToToken", data={"uid": _pending["uid"], "code_verifier": _pending["verifier"]})
    d = r.json()
    if not _ok(d):
        raise RuntimeError(f"换取令牌失败：{d.get('message') or d.get('error') or d}")
    t = d["data"]
    settings.set_internal(p115_access=t["access_token"], p115_refresh=t["refresh_token"],
                          p115_expires=time.time() + int(t.get("expires_in", 7200)))
    _pending.clear()
    return {"status": 2, "done": True}


def auth_status() -> dict:
    s = settings.get()
    mode = "open" if s["p115_refresh"] else ("cookie" if cfg.P115_COOKIE else "none")
    return {"authorized": bool(s["p115_refresh"]), "app_id_set": bool(s["p115_app_id"]),
            "cookie": bool(cfg.P115_COOKIE), "mode": mode}


def auth_clear():
    settings.set_internal(p115_access="", p115_refresh="", p115_expires=0)


class OpenDrive:
    def __init__(self):
        self._lk = asyncio.Lock()

    async def _token(self) -> str:
        s = settings.get()
        if not s["p115_refresh"]:
            raise RuntimeError("还没有授权 115：请管理员到「设置 → 115 网盘」扫码授权")
        if s["p115_access"] and s["p115_expires"] - time.time() > 300:
            return s["p115_access"]
        async with self._lk:
            s = settings.get()
            if s["p115_access"] and s["p115_expires"] - time.time() > 300:  # 别的请求刚刷新过
                return s["p115_access"]
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(AUTH + "/open/refreshToken", data={"refresh_token": s["p115_refresh"]})
            d = r.json()
            if not _ok(d):
                raise RuntimeError(f"刷新 115 令牌失败，可能需要重新授权：{d.get('message') or d}")
            t = d["data"]
            settings.set_internal(p115_access=t["access_token"], p115_refresh=t.get("refresh_token") or s["p115_refresh"],
                                  p115_expires=time.time() + int(t.get("expires_in", 7200)))
            return t["access_token"]

    async def _call(self, method: str, path: str, **kw) -> dict:
        for attempt in (0, 1):
            tok = await self._token()
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request(method, API + path, headers={"Authorization": "Bearer " + tok}, **kw)
            try:
                d = r.json()
            except Exception:  # noqa
                raise RuntimeError(f"115 返回异常（HTTP {r.status_code}）")
            if attempt == 0 and (r.status_code == 401 or str(d.get("code", "")).startswith("401401")):
                settings.set_internal(p115_expires=0)  # 令牌失效：强制刷新后重试一次
                continue
            if not _ok(d):
                raise RuntimeError(f"115 接口失败：{d.get('message') or d}")
            return d
        raise RuntimeError("115 令牌无效")

    async def _children(self, cid: int) -> list[dict]:
        out, off = [], 0
        while True:
            d = await self._call("GET", "/open/ufile/files", params={"cid": cid, "limit": 1150, "offset": off, "show_dir": 1})
            data = d.get("data") or []
            items = data if isinstance(data, list) else data.get("list", [])
            out += items
            if len(items) < 1150:
                return out
            off += len(items)

    @staticmethod
    def _is_dir(it) -> bool:
        return str(it.get("fc")) == "0"

    async def list_dirs(self, cid: int) -> list[dict]:
        return [{"id": int(i["fid"]), "name": i.get("fn") or i.get("file_name", "")}
                for i in await self._children(cid) if self._is_dir(i)]

    async def mkdir(self, parent: int, name: str) -> int:
        d = await self._call("POST", "/open/folder/add", data={"pid": parent, "file_name": name})
        return int(d["data"]["file_id"])

    async def ensure_dir(self, parent: int, name: str) -> int:
        for i in await self.list_dirs(parent):
            if i["name"] == name:
                return i["id"]
        return await self.mkdir(parent, name)

    async def list_files(self, cid: int) -> list[dict]:
        out = []
        for it in await self._children(cid):
            if self._is_dir(it):
                out += await self.list_files(int(it["fid"]))
            else:
                out.append({"id": it["fid"], "name": it.get("fn") or it.get("file_name", ""),
                            "size": int(it.get("fs") or it.get("size") or 0)})
        return out

    async def add_offline(self, magnet: str, dest: int) -> str:
        d = await self._call("POST", "/open/offline/add_task_urls", data={"urls": magnet, "wp_path_id": dest})
        rows = d.get("data") or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        if row and not row.get("state", True):
            raise RuntimeError(f"添加离线任务失败：{row.get('message') or row}")
        h = row.get("info_hash") or (re.search(r"btih:([0-9a-zA-Z]{32,40})", magnet) or [None, ""])[1]
        return h.lower()

    async def offline_state(self, info_hash: str) -> str:
        page = 1
        while True:
            d = await self._call("GET", "/open/offline/get_task_list", params={"page": page})
            data = d.get("data") or {}
            for t in data.get("tasks", []):
                if str(t.get("info_hash", "")).lower() == info_hash:
                    if t.get("status") == 2 or t.get("percentDone") == 100:
                        return "done"
                    return "failed" if t.get("status") == -1 else "running"
            if page >= int(data.get("page_count") or 1):
                return "running"
            page += 1

    async def move(self, ids, dest):
        await self._call("POST", "/open/ufile/move", data={"file_ids": ",".join(map(str, ids)), "to_cid": dest})

    async def delete(self, ids):
        await self._call("POST", "/open/ufile/delete", data={"file_ids": ",".join(map(str, ids))})


class CompositeDrive:
    """已授权开放平台就走 Open；否则用 Cookie；分享链接转存始终需要 Cookie。"""

    def __init__(self):
        self.open = OpenDrive()
        self._cookie, self._ck = "", None

    def _cookie_drive(self) -> "P115Drive":
        if cfg.P115_COOKIE != self._cookie or self._ck is None:
            self._ck, self._cookie = P115Drive(cfg.P115_COOKIE), cfg.P115_COOKIE
        return self._ck

    def backend(self):
        if settings.get()["p115_refresh"]:
            return self.open
        if cfg.P115_COOKIE:
            return self._cookie_drive()
        raise RuntimeError("还没有登录 115：请在「设置 → 115 网盘」填写 Cookie，或填 AppID 后扫码授权，并先点「保存设置」")

    def mode(self) -> str:
        return auth_status()["mode"]

    def can_receive_share(self) -> bool:
        return bool(cfg.P115_COOKIE)

    async def receive_share(self, share_code, receive_code, dest):
        if not cfg.P115_COOKIE:
            raise RuntimeError("没有配置 115 Cookie，无法转存分享链接")
        await self._cookie_drive().receive_share(share_code, receive_code, dest)

    def __getattr__(self, name):
        if name.startswith("__") or name in ("open", "_cookie", "_ck"):
            raise AttributeError(name)
        return getattr(self.backend(), name)
