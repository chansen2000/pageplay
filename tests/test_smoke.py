"""冒烟测试：包与全部模块可导入，CLI list 命令可执行并返回 0。"""


def test_all_modules_importable() -> None:
    import pageplay
    import pageplay.cli
    import pageplay.sites
    import pageplay.cookies
    import pageplay.session
    import pageplay.guard
    import pageplay.logging_setup

    # 只验证版本元数据存在且非空——不钉死具体值，避免每次发版误报
    assert isinstance(pageplay.__version__, str) and pageplay.__version__


def test_list_returns_zero() -> None:
    from pageplay.cli import main

    assert main(["list"]) == 0
