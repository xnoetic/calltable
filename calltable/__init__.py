"""calltable——验证任务排队系统：hub + 执行器 + CLI + 看板（仅标准库）。"""

__version__ = "0.2.0"

#: 引入 profile ``rules`` 的客户端版本。更旧的客户端在 ``POST /api/jobs`` 里
#: 带上来的 slot/locks/timeout 是「profile 默认值当显式值」，hub 会忽略并按
#: rules 重新计算；新客户端显式写的参数仍然优先。
RULES_CLIENT_VERSION = "0.2.0"
