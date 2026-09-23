"""cookie 快照：登录态判定、按域过滤、快照读写、精简导出。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

_EXPORT_KEYS = ("name", "value", "domain", "expires")


def has_login_state(cookies: list[dict], domain: str, check_cookies: tuple[str, ...]) -> bool:
    """判定 cookie 列表是否携带登录态。

    check_cookies 非空：指定标记 cookie 出现在该域即算已登录；
    check_cookies 为空 tuple：该域存在任意 cookie 即算已登录。
    """
    scoped = filter_by_domain(cookies, domain)
    if not check_cookies:
        return bool(scoped)
    present = {c.get("name") for c in scoped}
    return all(name in present for name in check_cookies)


def filter_by_domain(cookies: list[dict], domain: str) -> list[dict]:
    """按域过滤 cookie：cookie["domain"] 含 domain 子串即保留。"""
    return [c for c in cookies if domain in c.get("domain", "")]


def save_snapshot(site_dir: Path, cookies: list[dict]) -> Path:
    """把 cookie 落成站点快照并返回快照文件路径。

    写 <site_dir>/state.json，结构：
    {"version": 1, "site": ..., "saved_at": ..., "cookies": [...]}
    文件权限 0600。
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    path = site_dir / "state.json"
    payload = {
        "version": 1,
        "site": site_dir.name,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cookies": cookies,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows 等无 POSIX 权限位环境：静默跳过
        pass
    return path


def load_snapshot(site_dir: Path) -> list[dict]:
    """读取站点快照，返回 cookies 列表。

    快照文件缺失：raise FileNotFoundError，消息带"先 pageplay login"指引。
    """
    path = site_dir / "state.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"登录态快照不存在：{path}\n先 pageplay login {site_dir.name} 登录一次再试"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cookies"]


def export_json(cookies: list[dict]) -> str:
    """导出精简 JSON 字符串：每条 cookie 仅保留 name/value/domain/expires。"""
    slim = [{k: c[k] for k in _EXPORT_KEYS if k in c} for c in cookies]
    return json.dumps(slim, ensure_ascii=False, indent=2)
