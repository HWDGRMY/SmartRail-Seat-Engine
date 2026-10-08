"""HTTP 服务层。

* :mod:`smartrail.api.service`      —— 与框架无关的业务核心（零依赖）
* :mod:`smartrail.api.app`          —— FastAPI 实现（推荐部署方式）
* :mod:`smartrail.api.stdlib_server` —— 标准库备用实现（离线环境）

注意：这里**不**在包初始化时导入 ``app``，避免"没装 FastAPI 就用不了引擎"。
需要 FastAPI 应用对象时显式导入：

    from smartrail.api.app import app
"""

from . import service

__all__ = ["service"]
