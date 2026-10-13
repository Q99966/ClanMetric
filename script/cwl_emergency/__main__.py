"""支持通过 ``python -m cwl_emergency`` 启动命令行流程。"""

from .cli import main

raise SystemExit(main())
