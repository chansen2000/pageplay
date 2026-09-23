"""冒烟测试：包与全部模块可导入，CLI 未实现命令返回 2。"""


def test_all_modules_importable() -> None:
    import pageplay
    import pageplay.cli
    import pageplay.sites
    import pageplay.cookies
    import pageplay.session
    import pageplay.guard
    import pageplay.logging_setup

    assert pageplay.__version__ == "0.1.0"


def test_list_returns_not_implemented_code() -> None:
    from pageplay.cli import main

    assert main(["list"]) == 2
