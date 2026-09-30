"""115 驱动。流程只依赖 Drive115 这几个方法，方便替换/测试。

注意：P115Drive 基于 p115client 和 115 网页接口的已知参数写成，
开发环境无法联网，尚未用真实账号联调；请先调用 /api/selftest 验证。
"""
import asyncio
import re
from typing import Protocol


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


class LazyDrive:
    """首次使用时才创建 115 客户端；没配 Cookie 时应用照常启动，用到时才报错。"""

    def __init__(self, cookie: str):
        self._cookie, self._d = cookie, None

    def __getattr__(self, name):
        if not self._cookie:
            raise RuntimeError("未配置 P115_COOKIE")
        if self._d is None:
            self._d = P115Drive(self._cookie)
        return getattr(self._d, name)


class P115Drive:
    def __init__(self, cookie: str):
        from p115client import P115Client
        self.c = P115Client(cookie)

    async def _run(self, fn, *a, **kw):
        return _check(await asyncio.to_thread(fn, *a, **kw))

    async def _children(self, cid: int) -> list[dict]:
        r = await self._run(self.c.fs_files, {"cid": cid, "limit": 1000, "show_dir": 1})
        return r.get("data", [])

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
