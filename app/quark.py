"""夸克网盘驱动（Cookie 方式，走网页版接口）。负责：转存分享链接、建目录、列目录、移动、改名、删除。
夸克没有磁力/离线下载，所以只能转存分享链接。

接口按社区常见用法（quark-auto-save 等项目）写成，开发环境无法联网，没有用真实账号验证过；
先用设置页「测试夸克连接」确认，再手动求一部片试。
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import cfg

BASE = "https://drive-pc.quark.cn/1/clouddrive"
QS = {"pr": "ucpro", "fr": "pc"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 quark-cloud-drive/3.14.2")
SHARE_QUARK = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+[^\s\"'<>)\]]*")


def parse_quark_share(url: str, password: str = ""):
    """→ (pwd_id, passcode) 或 None。支持 https://pan.quark.cn/s/abc?pwd=1234 和 …#/list/share/…"""
    u = urlsplit(url)
    m = re.search(r"/s/([A-Za-z0-9]+)", u.path)
    if not m or "quark.cn" not in u.netloc:
        return None
    pw = password or (parse_qs(u.query).get("pwd") or parse_qs(u.query).get("passcode") or [""])[0]
    return m.group(1), pw


def configured() -> bool:
    return bool(cfg.QUARK_COOKIE and str(cfg.QUARK_STAGING_FID or "").strip("0") and str(cfg.QUARK_LIBRARY_FID or "").strip("0"))


class QuarkError(RuntimeError):
    pass


class QuarkDrive:
    def __init__(self):
        self._c = httpx.AsyncClient(timeout=30, follow_redirects=True)

    def _headers(self) -> dict:
        return {"Cookie": cfg.QUARK_COOKIE, "Content-Type": "application/json", "User-Agent": UA,
                "Referer": "https://pan.quark.cn/", "Origin": "https://pan.quark.cn"}

    async def _req(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        if not cfg.QUARK_COOKIE:
            raise QuarkError("还没有配置夸克 Cookie")
        r = await self._c.request(method, BASE + path, params={**QS, **(params or {})}, json=json, headers=self._headers())
        try:
            j = r.json()
        except Exception:  # noqa
            raise QuarkError(f"夸克返回的不是 JSON（HTTP {r.status_code}），Cookie 可能已失效")
        if j.get("code") not in (0, None) or (r.status_code >= 400 and not j.get("data")):
            msg = j.get("message") or j.get("msg") or str(j)[:100]
            if r.status_code in (401, 403) or "login" in str(msg).lower() or j.get("code") in (31001,):
                msg = f"{msg}（Cookie 可能已失效，请重新复制）"
            raise QuarkError(f"夸克接口失败：{msg}")
        return j

    async def _wait_task(self, task_id: str, tries: int = 60) -> dict:
        for i in range(tries):
            j = await self._req("GET", "/task", {"task_id": task_id, "retry_index": i})
            d = j.get("data") or {}
            if d.get("status") == 2:
                return d
            if d.get("status") not in (None, 0, 1):
                raise QuarkError(f"夸克任务失败：{d.get('message') or d}")
            await asyncio.sleep(0.6)
        raise QuarkError("夸克任务超时")

    # ---------------------------------------------------------------- 目录
    async def _children(self, fid: str) -> list[dict]:
        out, page = [], 1
        while True:
            j = await self._req("GET", "/file/sort", {"pdir_fid": fid, "_page": page, "_size": 100, "_fetch_total": 1,
                                                       "_sort": "file_type:asc,updated_at:desc"})
            items = (j.get("data") or {}).get("list", [])
            out += items
            if len(items) < 100:
                return out
            page += 1

    @staticmethod
    def _is_dir(it) -> bool:
        return bool(it.get("dir")) or it.get("file_type") == 0

    async def list_dirs(self, fid) -> list[dict]:
        return [{"id": it["fid"], "name": it.get("file_name", "")} for it in await self._children(str(fid)) if self._is_dir(it)]

    async def mkdir(self, parent, name: str) -> str:
        j = await self._req("POST", "/file", json={"pdir_fid": str(parent), "file_name": name, "dir_path": "", "dir_init_lock": False})
        return (j.get("data") or {})["fid"]

    async def ensure_dir(self, parent, name: str) -> str:
        for d in await self.list_dirs(parent):
            if d["name"] == name:
                return d["id"]
        return await self.mkdir(parent, name)

    async def list_files(self, fid, _prefix: str = "") -> list[dict]:
        out = []
        for it in await self._children(str(fid)):
            nm = it.get("file_name", "")
            if self._is_dir(it):
                out += await self.list_files(it["fid"], _prefix + nm + "/")
            else:
                out.append({"id": it["fid"], "name": nm, "size": int(it.get("size") or 0), "path": _prefix + nm})
        return out

    # ---------------------------------------------------------------- 文件操作
    async def _fallback_move_delete(self, path: str, ids: list, extra: dict) -> dict:
        """批量移动/删除接口新旧两版参数不同：新版用 filelist（fid 字符串数组），旧版用 action_type+filter_fids。
        先按新版发，报参数错误再回退旧版。"""
        new = {"filelist": [str(i) for i in ids], **extra}
        try:
            return await self._req("POST", path, json=new)
        except QuarkError:
            old = {"action_type": 2, "filter_fids": [str(i) for i in ids], **extra}
            return await self._req("POST", path, json=old)

    async def move(self, ids, dest):
        j = await self._fallback_move_delete("/file/move", ids, {"exclude_fids": [], "to_pdir_fid": str(dest)})
        if (j.get("data") or {}).get("task_id"):
            await self._wait_task(j["data"]["task_id"])

    async def delete(self, ids):
        j = await self._fallback_move_delete("/file/delete", ids, {"exclude_fids": []})
        if (j.get("data") or {}).get("task_id"):
            await self._wait_task(j["data"]["task_id"])

    async def rename(self, fid, new_name: str):
        await self._req("POST", "/file/rename", json={"fid": str(fid), "file_name": new_name})

    # ---------------------------------------------------------------- 转存分享
    def can_receive_share(self) -> bool:
        return True

    async def receive_share(self, pwd_id: str, passcode: str, dest) -> None:
        j = await self._req("POST", "/share/sharepage/token", json={"pwd_id": pwd_id, "passcode": passcode or ""})
        stoken = (j.get("data") or {}).get("stoken")
        if not stoken:
            raise QuarkError("分享已失效或提取码不对")
        fids, tokens, page = [], [], 1
        while True:
            j = await self._req("GET", "/share/sharepage/detail", {"pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "_page": page,
                                                                    "_size": 50, "_fetch_banner": 0, "_fetch_share": 0, "_fetch_total": 1,
                                                                    "_sort": "file_type:asc,updated_at:desc"})
            items = (j.get("data") or {}).get("list", [])
            fids += [it["fid"] for it in items]
            tokens += [it["share_fid_token"] for it in items]
            if len(items) < 50:
                break
            page += 1
        if not fids:
            raise QuarkError("分享为空或已失效")
        # 夸克新旧两版转存接口参数不同：新版用 current_dir_fid + filelist（元素是 {fid, share_fid_token} 对象），
        # 旧版用 fid_list + fid_token_list；接口改版后旧参数被视为空，报「current_dir_fid/filelist 不能同时为空」。
        # 先按新版发，响应里没有 task_id 再回退旧版参数。
        files = [{"fid": f, "share_fid_token": t} for f, t in zip(fids, tokens)]
        payload = {"current_dir_fid": "0", "filelist": files, "to_pdir_fid": str(dest),
                   "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "scene": "link"}
        try:
            j = await self._req("POST", "/share/sharepage/save", json=payload)
        except QuarkError:  # 新版参数不被当前接口接受时尝试旧版参数
            j = await self._req("POST", "/share/sharepage/save",
                                json={"fid_list": fids, "fid_token_list": tokens, "to_pdir_fid": str(dest),
                                      "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "scene": "link",
                                      "on_dup": "ignore"})
        if not (j.get("data") or {}).get("task_id"):
            j = await self._req("POST", "/share/sharepage/save",
                                json={"fid_list": fids, "fid_token_list": tokens, "to_pdir_fid": str(dest),
                                      "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0", "scene": "link",
                                      "on_dup": "ignore"})
        await self._wait_task(j["data"]["task_id"])

    async def close(self):
        await self._c.aclose()


async def test() -> dict:
    d = QuarkDrive()
    try:
        dirs = await d.list_dirs("0")
        return {"ok": True, "root_dirs": len(dirs)}
    except Exception as e:  # noqa
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        await d.close()
