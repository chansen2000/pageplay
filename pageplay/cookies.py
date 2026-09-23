"""cookie 快照：登录态判定、按域过滤、快照读写、精简导出。"""

from __future__ import annotations

from pathlib import Path


def has_login_state(cookies: list[dict], domain: str, check_cookies: tuple[str, ...]) -> bool:
    """判定 cookie 列表是否携带登录态。

    check_cookies 非空：指定标记 cookie 出现在该域即算已登录；
    check_cookies 为空 tuple：该域存在任意 cookie 即算已登录。
    """
    raise NotImplementedError


def filter_by_domain(cookies: list[dict], domain: str) -> list[dict]:
    """按域过滤 cookie：cookie["domain"] 含 domain 子串即保留。"""
    raise NotImplementedError


def save_snapshot(site_dir: Path, cookies: list[dict]) -> Path:
    """把 cookie 落成站点快照并返回快照文件路径。

    写 <site_dir>/state.json，结构：
    {"version": 1, "site": ..., "saved_at": ..., "cookies": [...]}
    文件权限 0600。
    """
    raise NotImplementedError


def load_snapshot(site_dir: Path) -> list[dict]:
    """读取站点快照，返回 cookies 列表。

    快照文件缺失：raise FileNotFoundError，消息带"先 pageplay login"指引。
    """
    raise NotImplementedError


def export_json(cookies: list[dict]) -> str:
    """导出精简 JSON 字符串：每条 cookie 仅保留 name/value/domain/expires。"""
    raise NotImplementedError
