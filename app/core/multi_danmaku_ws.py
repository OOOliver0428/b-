"""⚠️ DEPRECATED: 此模块已弃用，功能已集成到 DanmakuClient 中。

请直接使用 app.core.danmaku_ws.DanmakuClient，它内置了多服务器连接机制。
此文件将在 v2.0.0 中删除。
"""
import warnings

warnings.warn(
    "multi_danmaku_ws.py 已弃用，请使用 danmaku_ws.DanmakuClient（内置多连接）",
    DeprecationWarning,
    stacklevel=2,
)

import asyncio
from typing import Callable, Optional, List, Dict
from loguru import logger

from app.core.danmaku_ws import DanmakuClient


class MultiDanmakuClient:
    """
    ⚠️ DEPRECATED: 多连接弹幕客户端

    此类已弃用。DanmakuClient 已内置多服务器连接机制。
    请直接使用 DanmakuClient。
    """

    def __init__(self, room_id: int, on_danmaku: Optional[Callable] = None):
        warnings.warn(
            "MultiDanmakuClient 已弃用，请使用 DanmakuClient（内置多连接）",
            DeprecationWarning,
            stacklevel=2,
        )
        self.room_id = room_id
        self.on_danmaku_callback = on_danmaku
        self.clients: List[DanmakuClient] = []
        self.running = False

    async def start(self) -> bool:
        """启动 - 委托给 DanmakuClient"""
        client = DanmakuClient(self.room_id, self.on_danmaku_callback)
        success = await client.start()
        if success:
            self.clients = [client]
            self.running = True
        return success

    async def stop(self):
        """停止所有客户端"""
        self.running = False
        for client in self.clients:
            if client.running:
                await client.stop()
        self.clients.clear()
        logger.info("多连接弹幕客户端已停止（已弃用）")
