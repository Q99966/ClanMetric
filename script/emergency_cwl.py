"""应急联赛命令行兼容入口；实际参数逻辑位于 cwl_emergency.cli。"""

from cwl_emergency.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
