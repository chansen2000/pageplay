"""vision 命令单测（T21a）：视觉解析纯函数、模型调用替身、CLI 集成。

零真实外网、零真浏览器：模型调用在 vision._post（urllib 收敛点）与
vision.screenshot_table 打桩；_post 本身用本地 ThreadingHTTPServer
（127.0.0.1，端口 0）验真实 urllib 路径与鉴权头，仍不出本机。CLI 集成
浏览器附着在 cli_vision 命名空间打桩（替身页同 grab：url/title 即够，
pick_target_page 用真的——替身页无 evaluate 按 hidden 回落取最后页），
落盘走真 actions.save_table、落账走真 runs.jsonl。
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pageplay.cli import main
from pageplay.cli_pick import _runs_path
from pageplay.guard import RiskTriggered
from pageplay.runs import load_runs
from pageplay import vision
from pageplay.vision import parse_vision_json, screenshot_table

from fakes_browser import fake_downloads_home

_KEY = "sk-test-glm-key-abc123"
_ROWS = [{"名称": "商品1", "价格": "10"}, {"名称": "商品2", "价格": "20"}]


# ----------------------------------------------------------------------
# parse_vision_json：裸 JSON / 围栏 / 各类不合规人话
# ----------------------------------------------------------------------

def test_parse_bare_json_returns_rows():
    """裸 JSON（模型守约没加围栏）：直接解析取 rows。"""
    assert parse_vision_json('{"rows": [{"a": "1"}]}') == [{"a": "1"}]


def test_parse_fenced_json_strips_fence():
    """```json 围栏与裸 ``` 围栏都剥掉再解析（模型最常见输出形状）。"""
    fenced = '```json\n' + json.dumps({"rows": _ROWS}, ensure_ascii=False) \
        + '\n```'
    assert parse_vision_json(fenced) == _ROWS
    plain = '```\n{"rows": []}\n```'
    assert parse_vision_json(plain) == []


@pytest.mark.parametrize("bad", ["", "   ", "好的，这是识别结果：没有表格",
                                 "not json at all"])
def test_parse_garbage_text_raises_human_valueerror(bad):
    """垃圾文本/空串：剥围栏后解析失败 → ValueError 人话（带实际开头）。"""
    with pytest.raises(ValueError, match="没有按要求输出 JSON"):
        parse_vision_json(bad)


@pytest.mark.parametrize("bad", ['{"rows": "x"}', '{"rows": 3}',
                                 '{"no_rows": []}', '[{"rows": []}]',
                                 '"rows"'])
def test_parse_wrong_shape_raises_human_valueerror(bad):
    """rows 缺失/非列表/顶层非对象：ValueError 人话点名问题。"""
    with pytest.raises(ValueError):
        parse_vision_json(bad)


def test_parse_row_items_must_be_dicts():
    """rows 里混入非对象行：ValueError 人话并回显那行。"""
    with pytest.raises(ValueError, match="不是对象的行"):
        parse_vision_json('{"rows": [{"a": "1"}, "坏行"]}')


# ----------------------------------------------------------------------
# _post：本地假服务验真实 urllib 路径（不出外网）
# ----------------------------------------------------------------------

class _VisionHandler(BaseHTTPRequestHandler):
    """bigmodel 形状的本地假服务：校验 Bearer 头与路径，回 choices JSON。"""

    def do_POST(self) -> None:
        if self.path != "/api/paas/v4/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("Authorization") != f"Bearer {_KEY}":
            self.send_response(401)  # 鉴权头没带对：真服务也是这个形状
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.server.fail_code:  # 注入 HTTP 错误码用
            self.send_response(self.server.fail_code)
            self.end_headers()
            return
        body = json.dumps({"choices": [
            {"message": {"content": "{\"rows\": []}"}}]},
            ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默：不把假服务访问日志刷进测试输出


@pytest.fixture
def vision_server(monkeypatch):
    """本地假 bigmodel：ENDPOINT 指过去，测完关停；server.fail_code 注错误。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VisionHandler)
    server.fail_code = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(vision, "ENDPOINT",
                        f"http://127.0.0.1:{server.server_address[1]}"
                        "/api/paas/v4/chat/completions")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_post_success_and_auth_header(vision_server):
    """200 + 鉴权头正确：返回 choices[0].message.content 文本。"""
    assert vision._post({"model": "m"}, _KEY) == "{\"rows\": []}"


def test_post_http_500_runtimeerror_without_key_leak(vision_server):
    """HTTP 500：RuntimeError 人话（点名 HTTP 码），消息不含 key。"""
    vision_server.fail_code = 500
    with pytest.raises(RuntimeError) as exc_info:
        vision._post({"model": "m"}, _KEY)
    assert "HTTP 500" in str(exc_info.value)
    assert _KEY not in str(exc_info.value)  # 脱敏：key 不进任何错误消息


# ----------------------------------------------------------------------
# screenshot_table：_post 打桩验 payload 组装与 key 口径
# ----------------------------------------------------------------------

class ShotPage:
    """截屏替身：screenshot(type="png") 返回固定字节。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def screenshot(self, type: str = "png") -> bytes:  # noqa: A002
        self.calls.append({"type": type})
        return b"png-bytes"


def test_screenshot_table_success_builds_payload(monkeypatch):
    """成功路：png→base64 data url + PROMPT + 缺省模型 + temperature。"""
    posts: list[tuple[dict, str]] = []

    def fake_post(payload: dict, api_key: str) -> str:
        posts.append((payload, api_key))
        return json.dumps({"rows": _ROWS}, ensure_ascii=False)

    monkeypatch.setenv("GLM_API_KEY", _KEY)
    monkeypatch.setattr(vision, "_post", fake_post)

    rows = screenshot_table(ShotPage())

    assert rows == _ROWS
    assert posts[0][1] == _KEY  # key 原样传给请求层
    payload = posts[0][0]
    assert payload["model"] == "glm-4v-plus"  # 缺省视觉模型
    assert payload["temperature"] == 0.1
    image_part, text_part = payload["messages"][0]["content"]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"] == \
        "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
    assert text_part == {"type": "text", "text": vision.PROMPT}


def test_screenshot_table_model_override(monkeypatch):
    """model 入参覆盖缺省模型（换模型不改编码）。"""
    seen: list[str] = []

    def fake_post(payload: dict, api_key: str) -> str:
        seen.append(payload["model"])
        return "{\"rows\": []}"

    monkeypatch.setenv("GLM_API_KEY", _KEY)
    monkeypatch.setattr(vision, "_post", fake_post)
    assert screenshot_table(ShotPage(), model="glm-4v-flash") == []
    assert seen == ["glm-4v-flash"]


def test_screenshot_table_missing_key_valueerror(monkeypatch):
    """GLM_API_KEY 未设且未显式传：ValueError 人话指引，且不发请求。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    def boom(payload, api_key):  # 缺 key 不该走到请求层
        raise AssertionError("缺 key 不应发起请求")

    monkeypatch.setattr(vision, "_post", boom)
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        screenshot_table(ShotPage())


def test_screenshot_table_explicit_key_wins(monkeypatch):
    """显式传 api_key 优先于环境变量（参数 > 环境的常规口径）。"""
    seen: list[str] = []
    monkeypatch.setenv("GLM_API_KEY", "env-key")
    monkeypatch.setattr(vision, "_post",
                        lambda p, k: seen.append(k) or "{\"rows\": []}")
    assert screenshot_table(ShotPage(), api_key=_KEY) == []
    assert seen == [_KEY]


# ----------------------------------------------------------------------
# CLI 集成：附着替身浏览器 + screenshot_table 打桩，真落盘真落账
# ----------------------------------------------------------------------

class VisionPage:
    """当前页替身：title()/url 即够（识别在 screenshot_table 打桩）。"""

    def __init__(self, url: str = "https://demo.example.com/list",
                 title: str = "列表页") -> None:
        self.url = url
        self._title = title

    def title(self) -> str:
        return self._title


class PagesContext:
    def __init__(self, pages: list) -> None:
        self.pages = pages


class PagesBrowser:
    def __init__(self, pages: list) -> None:
        self.contexts = [PagesContext(pages)]


def install_vision_browser(monkeypatch, pages: list) -> list[bool]:
    """cli_vision 的浏览器附着换成替身，返回记录的 headless 实参列表。"""
    headless_calls: list[bool] = []
    browser = PagesBrowser(pages)

    def fake_ensure(headless: bool = False):
        headless_calls.append(headless)
        return browser

    monkeypatch.setattr("pageplay.cli_vision.ensure_browser", fake_ensure)
    return headless_calls


def stub_vision(monkeypatch, rows: list[dict]) -> list:
    """screenshot_table 打桩：返回给定行，记录收到的 page。"""
    seen: list = []

    def fake_table(page, api_key=None, model=None):
        seen.append(page)
        return rows

    monkeypatch.setattr("pageplay.vision.screenshot_table", fake_table)
    return seen


def test_vision_success_saves_csv_and_records(home_dir, tmp_path, monkeypatch,
                                              capsys):
    """识别到 2 行：CSV+JSON 落 vision 目录，账本记 vision ok，退出码 0。"""
    fake_home = fake_downloads_home(monkeypatch, tmp_path)
    page = VisionPage()
    install_vision_browser(monkeypatch, [page])
    seen = stub_vision(monkeypatch, _ROWS)

    assert main(["vision", "taobao"]) == 0

    assert seen == [page]  # 选中的当前页原样交给视觉层
    out = capsys.readouterr().out
    assert "将截取该页画面交给视觉模型识别：列表页" in out
    assert "✓ 视觉抓取 2 行" in out
    vision_root = fake_home / "Downloads" / "pageplay" / "vision"
    (csv_path,) = vision_root.glob("taobao-vision-*.csv")
    assert csv_path.with_suffix(".json").is_file()
    assert csv_path.read_text(encoding="utf-8-sig").splitlines()[0] \
        == "名称,价格"
    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "taobao-vision" and entry["action"] == "vision"
    assert entry["status"] == "ok" and len(entry["outputs"]) == 2


def test_vision_empty_rows_exit_one_fail_record(home_dir, tmp_path,
                                                monkeypatch, capsys):
    """模型说没有表格：人话 ✗ + fail 账（outputs 空），退出码 1。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])
    stub_vision(monkeypatch, [])

    assert main(["vision", "taobao"]) == 1

    assert "✗ 视觉模型没有识别到表格" in capsys.readouterr().out
    (entry,) = load_runs(_runs_path())
    assert entry["status"] == "fail" and entry["outputs"] == []


def test_vision_no_site_labels_from_page_url(home_dir, tmp_path, monkeypatch):
    """不带站点参数：命名从当前页 URL 推（item.taobao.com → taobao）。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch,
                           [VisionPage("https://item.taobao.com/s?ie=utf8")])
    stub_vision(monkeypatch, _ROWS)

    assert main(["vision"]) == 0

    (entry,) = load_runs(_runs_path())
    assert entry["recipe"] == "taobao-vision"  # 域名推导只影响命名


def test_vision_custom_out_dir(home_dir, tmp_path, monkeypatch):
    """--out 指到别处：产物落指定目录（缺省才用 ~/Downloads/pageplay/vision）。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])
    stub_vision(monkeypatch, _ROWS)
    out_dir = tmp_path / "vp"

    assert main(["vision", "taobao", "--out", str(out_dir)]) == 0

    (csv_path,) = out_dir.glob("taobao-vision-*.csv")
    assert csv_path.is_file()


def test_vision_missing_key_human_message_exit_one(home_dir, tmp_path,
                                                   monkeypatch, capsys):
    """GLM_API_KEY 未设：main 人话映射「执行失败：视觉抓取需要
    GLM_API_KEY …」，退出码 1（走真 screenshot_table，不发请求）。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    assert main(["vision", "taobao"]) == 1

    err = capsys.readouterr().err
    assert "视觉抓取需要 GLM_API_KEY 环境变量（bigmodel 密钥），设置后重试" \
        in err
    assert load_runs(_runs_path()) == []  # 配置问题不落账（没产生执行）


def test_vision_service_error_exit_one(home_dir, tmp_path, monkeypatch,
                                       capsys):
    """服务异常（RuntimeError）：main 映射「执行失败：…」，退出码 1。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])

    def boom(page, api_key=None, model=None):
        raise RuntimeError("连不上视觉服务；请检查网络后重试")

    monkeypatch.setattr("pageplay.vision.screenshot_table", boom)

    assert main(["vision", "taobao"]) == 1
    assert "连不上视觉服务" in capsys.readouterr().err


def test_vision_risk_exits_two(home_dir, tmp_path, monkeypatch):
    """真风控从视觉路径穿透：RiskTriggered 不吞，main 映射退出码 2。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [VisionPage()])

    def risk(page, api_key=None, model=None):
        raise RiskTriggered("响应命中风控关键词：滑块")

    monkeypatch.setattr("pageplay.vision.screenshot_table", risk)

    assert main(["vision", "taobao"]) == 2
    assert load_runs(_runs_path()) == []  # 风控穿透，不落 fail 账


def test_vision_zero_pages_human_message_exit_one(home_dir, tmp_path,
                                                  monkeypatch, capsys):
    """没有任何标签页：pick_target_page 人话指引先开窗口，退出码 1。"""
    fake_downloads_home(monkeypatch, tmp_path)
    install_vision_browser(monkeypatch, [])

    assert main(["vision"]) == 1

    assert "先打开窗口" in capsys.readouterr().err
