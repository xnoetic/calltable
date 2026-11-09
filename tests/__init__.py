"""单元测试与端到端测试。

导入本包时就把用户级路径隔离到临时目录，任何测试（及其子进程）都看不到真实
的 ``~/.config/calltable/config.json``；详见 :mod:`tests.isolation`。
"""

from .isolation import apply

apply()
