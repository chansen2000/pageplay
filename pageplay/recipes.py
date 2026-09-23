"""recipe 纯数据层：字段校验、按名存取、清单。pick 保存与 run 执行共用。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("version", "name", "site", "url", "action", "selector")
_ACTIONS = ("download", "table")
_LIST_KEYS = ("name", "action", "url", "created_at")
_RECIPES_DIRNAME = "recipes"


def _recipe_path(site_dir: Path, name: str) -> Path:
    """recipe 文件路径 <site_dir>/recipes/<name>.json；名字先过安全校验。

    recipe 名直接拼进文件路径，不校验就能用 "../x" 或 "..\\x" 写穿到
    recipes 目录外，所以拒收：空名、"."、含 "/" 或 "\\" 或 ".." 的名字。
    """
    name = str(name)
    if (not name.strip() or name == "."
            or "/" in name or "\\" in name or ".." in name):
        raise ValueError(
            f"非法 recipe 名：{name!r}；"
            f"名字不能为空、不能是 . ，也不能含路径分隔符或 ..")
    return site_dir / _RECIPES_DIRNAME / f"{name}.json"


def save_recipe(site_dir: Path, recipe: dict) -> Path:
    """校验并把 recipe 落成 <site_dir>/recipes/<name>.json，返回文件路径。

    - REQUIRED_FIELDS 任一缺失/为 None/空白串 → ValueError（消息点名缺哪些）
    - action 只认 download / table，其他 → ValueError
    - 写入自动带 created_at（ISO 秒级）；recipe 已带则原样保留
    - 同名 recipe 直接覆盖（log.info 一句），不做多版本并存
    """
    missing = [f for f in REQUIRED_FIELDS
               if f not in recipe or recipe[f] is None
               or str(recipe[f]).strip() == ""]
    if missing:
        raise ValueError(
            f"recipe 缺少必填字段：{', '.join(missing)}"
            f"（必填：{', '.join(REQUIRED_FIELDS)}）")
    if recipe["action"] not in _ACTIONS:
        raise ValueError(
            f"非法 action：{recipe['action']!r}；只支持 {' / '.join(_ACTIONS)}")

    path = _recipe_path(site_dir, recipe["name"])
    payload = dict(recipe)
    if not payload.get("created_at"):
        payload["created_at"] = datetime.now().isoformat(timespec="seconds")

    existed = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    if existed:
        log.info("recipe %s 已存在，覆盖写入 %s", recipe["name"], path)
    return path


def load_recipe(site_dir: Path, name: str) -> dict:
    """按名读 recipe，返回 JSON 全量 dict。

    文件缺失 → FileNotFoundError，消息带"先 pageplay pick"指引
    和现有 recipe 清单，方便挑名字或发现还没保存过。
    """
    path = _recipe_path(site_dir, name)
    if not path.is_file():
        names = [r["name"] for r in list_recipes(site_dir)]
        known = "、".join(str(n) for n in names) if names else "（一个都没有）"
        raise FileNotFoundError(
            f"recipe 不存在：{path}\n"
            f"先 pageplay pick 保存一个 recipe 再执行；现有 recipe：{known}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_recipes(site_dir: Path) -> list[dict]:
    """列站点下全部 recipe：按 name 排序，每条只留 name/action/url/created_at。

    recipes 目录不存在 → 空列表；单个文件损坏/非 dict → log.warning
    跳过，不让一份坏文件炸掉整个清单。
    """
    recipes_dir = site_dir / _RECIPES_DIRNAME
    if not recipes_dir.is_dir():
        return []
    out: list[dict] = []
    for path in recipes_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("recipe 文件损坏，已跳过：%s", path)
            continue
        if not isinstance(data, dict):
            log.warning("recipe 文件内容不是对象，已跳过：%s", path)
            continue
        out.append({k: data.get(k) for k in _LIST_KEYS})
    out.sort(key=lambda r: str(r["name"]))
    return out
