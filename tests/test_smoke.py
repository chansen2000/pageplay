"""冒烟测试：包与全部模块可导入，CLI list 命令可执行并返回 0。"""


def test_all_modules_importable() -> None:
    import pageplay
    import pageplay.cli
    import pageplay.sites
    import pageplay.cookies
    import pageplay.session
    import pageplay.guard
    import pageplay.logging_setup

    assert pageplay.__version__ == "0.1.0"


def test_list_returns_zero() -> None:
    from pageplay.cli import main

    assert main(["list"]) == 0
