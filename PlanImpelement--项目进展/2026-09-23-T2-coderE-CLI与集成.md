# T2 coder-E：CLI 接通 + 集成测试 + README

日期：2026-09-23。负责：coder-E（T2）。前置：T0 骨架 + T1 全部模块就位（61 passed，git 停在 63c68e6）。

## 本次改动

| 文件 | 改动 | 为什么 |
|------|------|--------|
| `pageplay/cli.py` | 全量填充：六命令处理函数接通 sites/session/cookies 模块；doctor/export/open/forget 补 `<site>` 位置参数（T0 骨架漏注册，设计 §4 命令面要求）；顶层异常映射 RiskTriggered→2 / KeyboardInterrupt→130 / 其他→1 | T0 只有 argparse 壳，处理函数全是 NotImplementedError |
| `tests/conftest.py` | 新建：`fake_site`（threading HTTPServer 假登录站：GET /login 表单、POST 发 sessionid=test123 并 302 /home）、`home_dir`（PAGEPLAY_HOME→tmp）、autouse 隔离夹具 | 集成/CLI 测试的公共夹具；保证任何测试不碰真实 ~/.pageplay |
| `tests/test_cli.py` | 新建 8 用例：list 空/已存、login 超时/成功（替身 SiteSession，不起真浏览器）、export 无快照、forget 缺目录、RiskTriggered→2、非法站点名→1 | CLI 派发层退出码映射全覆盖；真浏览器流程归集成测试 |
| `tests/test_integration.py` | 新建 2 用例：`test_login_doctor_export_chain`（真 chromium：persistent context 种 cookie→档案移入 PAGEPLAY_HOME→手写 meta.json→doctor→export 全链）；`test_export_to_out_file_content_and_permissions`（--out 文件内容+0600 权限） | 设计 §6 要求的 verify→export 真链路 |
| `tests/test_smoke.py` | `main(["list"])` 断言 2→0（T0 遗留"未实现返回 2"已过期），函数改名 `test_list_returns_zero` | list 已实现，任务指定本文件为唯一可动既有测试 |
| `README.md` | 重写为 98 行短文档：定位/工作模型/安装/六命令示例/存储与 PAGEPLAY_HOME/安全边界 | T2 交付物 |

## 关键设计点（实现决策）

1. **站点解析双通道**（cli.py:36 `_resolve_saved`）：非 login 命令先查内置表，未命中回退已存 `meta.json` 重建预设（login_url→domain 取 hostname 后两段，check_cookies 从 meta 读）。否则 login --url 保存的自定义站点 doctor/export 无法按名操作，集成链 `doctor faketest` 会断。
2. **doctor 成功后刷新快照**（cli.py:150）：依据设计 §3"快照只在 login/doctor 成功后刷新"。verify() 本身只验活不落快照，不补这一步则 doctor→export 链断（export 只读 state.json）。
3. **CLI 层不测真浏览器**（test_cli.py 模块 docstring）：login/doctor 的浏览器路径用替身 SiteSession 打桩，login 轮询本体已由 test_session 覆盖，真链路归 test_integration。

## 环境备注

- venv playwright 1.63.0；本机 ms-playwright 缓存缺 `chromium_headless_shell-1243`（1.63 headless 启动依赖），首次全量跑集成链如实 skip（70 passed, 1 skipped）。
- `playwright install chromium` 走本地代理（127.0.0.1:6152）下载 94.3 MiB 成功落地。
- 落地后集成链真跑暴露一个坑：种 cookie 不带 `expires` 是会话级 cookie，Chromium 关进程不落盘，搬档案后登录态丢失 → doctor 报过期。已修（test_integration.py 种 cookie 加 30 天 expires），重跑 71 passed。

## Phase 状态

- [x] T2 coder-E：CLI 接通 + 集成测试 + README（2026-09-23，全量 71 passed 0 skipped）
