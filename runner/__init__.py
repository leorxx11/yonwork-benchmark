"""YonWork Host API 基准测试驱动。

分层：discovery/transport/client 只负责发请求、收原材料、记时间窗；
assertions 负责判定；report 负责落盘与汇总。判定逻辑不得写回驱动层。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
