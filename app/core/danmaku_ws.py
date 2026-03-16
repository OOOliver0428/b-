"""B 站直播弹幕 WebSocket 客户端（优化版）
优化内容：
- 消息去重（基于 dm_v2 ID）
- 心跳优化（30 秒间隔 + 响应处理）
- 指数退避重连机制
- 消息队列缓冲
"""
import asyncio
import json
import struct
import zlib
import brotli
import time
from typing import Callable, Optional, Dict, Any, Set, List
from collections import deque
from loguru import logger
import websockets
from websockets.legacy.client import WebSocketClientProtocol

from app.core.bili_client import bili_client


class DanmakuClient:
    """
    B 站直播弹幕 WebSocket 客户端
    协议说明：
    - 使用 protobuf 编码（简化处理，直接用 JSON 解析）
    - 心跳包每 30 秒发送一次
    - 认证包包含 uid, roomid, protover, platform, type, key
    """
    
    # WebSocket 地址
    WS_URL = "wss://broadcastlv.chat.bilibili.com/sub"
    
    # 协议版本
    PROTOCOL_VERSION = 3  # 使用 brotli 压缩
    
    # 数据包类型
    PACKET_TYPE_HEARTBEAT = 2
    PACKET_TYPE_HEARTBEAT_RSP = 3
    PACKET_TYPE_NORMAL = 5
    PACKET_TYPE_AUTH = 7
    PACKET_TYPE_AUTH_RSP = 8
    
    # 心跳间隔（秒）
    HEARTBEAT_INTERVAL = 30
    
    def __init__(self, room_id: int, on_danmaku: Optional[Callable] = None):
        self.room_id = room_id
        self.real_room_id: Optional[int] = None
        self.token: Optional[str] = None
        self.ws_list: List[WebSocketClientProtocol] = []  # 多个 WebSocket 连接
        self.host_list: List[Dict] = []  # 服务器列表
        self.on_danmaku_callback = on_danmaku
        self.running = False
        self.uid = 0  # 0 表示匿名用户
        self._tasks: List[asyncio.Task] = []  # 所有任务
        
        # ===== 新增：消息去重 =====
        self.seen_msg_ids: deque = deque(maxlen=10000)  # 使用 deque 自动清理
        self.msg_id_ttl = 300  # 5 分钟过期（秒）
        self.last_msg_id_cleanup = time.time()
        
        # ===== 新增：消息队列缓冲 =====
        self.msg_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._queue_task: Optional[asyncio.Task] = None
        
        # ===== 新增：连接状态跟踪 =====
        self._fatal_error: Optional[str] = None  # 致命错误信息（如认证失败）
        self._max_reconnect_per_server = 3  # 每个服务器最大重连次数
        self._server_status: Dict[int, Dict] = {}  # 各服务器连接状态
        
    async def init_room(self) -> bool:
        """初始化直播间信息
        返回: True=初始化成功, False=初始化失败（房间不存在或其他错误）
        """
        # 获取真实房间 ID
        room_info = await bili_client.get_room_info(self.room_id)
        if not room_info:
            logger.error(f"房间 {self.room_id} 不存在或无法访问")
            return False
        
        # 检查房间状态
        room_status = room_info.get("live_status", -1)
        if room_status == 0:
            logger.warning(f"房间 {self.room_id} 未开播，但尝试连接弹幕服务器")
        
        self.real_room_id = room_info.get("room_id")
        if not self.real_room_id:
            logger.error(f"获取房间真实ID失败：{self.room_id}")
            return False
        
        # 获取当前用户信息（用于 uid）
        user_info = await bili_client.get_user_info()
        if user_info:
            self.uid = user_info.get("mid", 0)
            logger.info(f"当前用户：{user_info.get('uname')}, uid={self.uid}")
        else:
            logger.warning("无法获取用户信息，使用匿名模式 (uid=0)")
            self.uid = 0
        
        # 获取弹幕服务器信息
        danmu_info = await bili_client.get_danmu_info(self.room_id)
        if not danmu_info:
            logger.error(f"获取弹幕服务器信息失败：{self.room_id}")
            return False
        
        self.token = danmu_info.get("token")
        if not self.token:
            logger.error(f"获取弹幕token失败：{self.room_id}")
            return False
        
        # 获取 host_list（多个服务器）
        self.host_list = danmu_info.get("host_list", [])
        if not self.host_list:
            logger.error("没有可用的弹幕服务器")
            return False
        
        logger.info(f"房间初始化成功：{self.real_room_id}, uid={self.uid}, token={self.token[:20]}...")
        logger.info(f"获取到 {len(self.host_list)} 个弹幕服务器：{[h['host'] for h in self.host_list]}")
        return True
    
    def _pack_data(self, data: bytes, packet_type: int) -> bytes:
        """打包数据"""
        # 包头长度 16 字节
        # 4 字节：包总长度
        # 2 字节：包头长度 (固定 16)
        # 2 字节：协议版本
        # 4 字节：包类型
        # 4 字节：序列号 (固定 1)
        
        header = struct.pack(">IHHII", 
            len(data) + 16,  # 总长度
            16,               # 头部长度
            self.PROTOCOL_VERSION,  # 协议版本
            packet_type,      # 包类型
            1                 # 序列号
        )
        return header + data
    
    def _unpack_data(self, data: bytes) -> list:
        """解包数据，返回消息列表"""
        messages = []
        offset = 0
        
        while offset < len(data):
            if len(data) - offset < 16:
                break
                
            # 解析包头
            total_len, header_len, proto_ver, packet_type, seq = struct.unpack(">IHHII", data[offset:offset+16])
            
            logger.debug(f"解包：total_len={total_len}, header_len={header_len}, proto_ver={proto_ver}, packet_type={packet_type}")
            
            if total_len < 16:
                logger.debug(f"包长度太小：{total_len}")
                offset += 16
                continue
            
            if offset + total_len > len(data):
                logger.debug(f"包长度超出：total_len={total_len}, offset={offset}, data_len={len(data)}")
                # 可能是分包，尝试按剩余长度处理
                total_len = len(data) - offset
            
            payload = data[offset+header_len:offset+total_len]
            
            # 解压（如果需要）
            if proto_ver == 2:
                # zlib 压缩
                try:
                    payload = zlib.decompress(payload)
                    logger.debug(f"zlib 解压后 payload 长度：{len(payload)}")
                except Exception as e:
                    logger.debug(f"zlib 解压失败：{e}")
            elif proto_ver == 3:
                # brotli 压缩，但某些小包可能没有压缩
                try:
                    # 先尝试 brotli 解压
                    decompressed = brotli.decompress(payload)
                    payload = decompressed
                    logger.debug(f"brotli 解压后 payload 长度：{len(payload)}")
                except Exception as e:
                    # 解压失败，可能是未压缩的小包，尝试直接解析
                    logger.debug(f"brotli 解压失败，尝试直接解析：{e}")
                    # 如果 payload 看起来像 JSON，直接用它
                    try:
                        json.loads(payload.decode('utf-8'))
                        logger.debug("payload 是有效 JSON，无需解压")
                    except:
                        pass  # 不是 JSON，保持原样
            
            # 处理不同类型的包
            if packet_type == self.PACKET_TYPE_NORMAL:  # 普通消息
                try:
                    # 解压后的数据可能包含多个 JSON 消息，循环解析所有
                    parse_offset = 0
                    msg_count_in_payload = 0
                    
                    while parse_offset < len(payload):
                        # 跳过非 JSON 字符（零字节、填充等）
                        while parse_offset < len(payload) and payload[parse_offset] != ord('{'):
                            parse_offset += 1
                        
                        if parse_offset >= len(payload):
                            break
                        
                        remaining = payload[parse_offset:]
                        
                        # 查找完整的 JSON（括号匹配）
                        brace_depth = 0
                        json_end = -1
                        in_string = False
                        escape = False
                        
                        for i in range(len(remaining)):
                            c = remaining[i]
                            if escape:
                                escape = False
                                continue
                            if c == ord('\\'):
                                escape = True
                                continue
                            if c == ord('"'):
                                in_string = not in_string
                                continue
                            if not in_string:
                                if c == ord('{'):
                                    brace_depth += 1
                                elif c == ord('}'):
                                    brace_depth -= 1
                                    if brace_depth == 0:
                                        json_end = i + 1
                                        break
                        
                        if json_end <= 0:
                            break  # 找不到完整 JSON
                        
                        try:
                            msg = json.loads(remaining[:json_end].decode('utf-8', errors='replace'))
                            messages.append(msg)
                            msg_count_in_payload += 1
                            parse_offset += json_end
                        except json.JSONDecodeError:
                            parse_offset += json_end  # 跳过这条，继续
                    
                    if msg_count_in_payload > 0:
                        logger.debug(f"从 payload 解析到 {msg_count_in_payload} 条消息")
                    
                except Exception as e:
                    logger.debug(f"解析消息失败：{e}")
            
            elif packet_type == self.PACKET_TYPE_AUTH_RSP:  # 认证响应
                try:
                    msg = json.loads(payload.decode('utf-8'))
                    messages.append({"cmd": "AUTH_REPLY", "data": msg})
                    logger.info(f"认证响应：{msg}")
                except Exception as e:
                    logger.debug(f"解析认证响应失败：{e}")
            
            elif packet_type == self.PACKET_TYPE_HEARTBEAT_RSP:  # 心跳响应
                # 心跳响应通常是一个整数（在线人数）
                try:
                    online_count = struct.unpack(">I", payload)[0]
                    logger.debug(f"心跳响应，在线人数：{online_count}")
                except:
                    pass
            
            offset += total_len
        
        return messages
    
    def _is_duplicate_msg(self, msg_id: str) -> bool:
        """检查消息是否重复"""
        if not msg_id:
            return False
        
        # 定期清理过期 ID
        current_time = time.time()
        if current_time - self.last_msg_id_cleanup > 60:  # 每分钟清理一次
            self._cleanup_msg_ids()
            self.last_msg_id_cleanup = current_time
        
        # 检查是否重复
        if msg_id in self.seen_msg_ids:
            return True
        
        # 添加到已见列表
        self.seen_msg_ids.append(msg_id)
        return False
    
    def _cleanup_msg_ids(self):
        """清理过期的消息 ID（基于时间）"""
        # deque 会自动限制大小，这里只需要记录时间即可
        # 如果需要更精确的 TTL，可以使用 dict 存储时间戳
        pass
    
    async def _send_auth(self, ws: WebSocketClientProtocol) -> bool:
        """发送认证包并等待响应"""
        auth_data = {
            "uid": self.uid,
            "roomid": self.real_room_id,
            "protover": self.PROTOCOL_VERSION,
            "platform": "web",
            "type": 2,
            "key": self.token,
        }
        data = json.dumps(auth_data).encode('utf-8')
        packet = self._pack_data(data, self.PACKET_TYPE_AUTH)
        await ws.send(packet)
        
        # 等待认证响应（5 秒超时）
        try:
            resp_data = await asyncio.wait_for(ws.recv(), timeout=5.0)
            messages = self._unpack_data(resp_data)
            
            # 检查是否有认证响应
            for msg in messages:
                if isinstance(msg, dict) and msg.get("cmd") == "AUTH_REPLY":
                    auth_data = msg.get("data", {})
                    if auth_data.get("code") == 0:
                        return True
                    else:
                        return False
            
            return True
            
        except asyncio.TimeoutError:
            return True  # 超时也继续，可能认证是静默的
        except websockets.exceptions.ConnectionClosed:
            return False  # 连接已关闭，认证失败
    
    async def _send_heartbeat(self, ws: WebSocketClientProtocol):
        """发送心跳包 - 30 秒间隔
        注意：不在这里接收响应，响应由 _listen 统一处理
        避免多个协程同时调用 ws.recv() 导致并发错误
        """
        while self.running:
            try:
                # 发送心跳
                packet = self._pack_data(b'[object Object]', self.PACKET_TYPE_HEARTBEAT)
                await ws.send(packet)
                logger.debug(f"[房间{self.room_id}] 心跳已发送")
                
                # 等待 30 秒（不接收响应，让 _listen 处理）
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            except Exception as e:
                logger.debug(f"[房间{self.room_id}] 心跳发送失败：{e}")
                break
    
    async def _listen(self, ws: WebSocketClientProtocol):
        """监听消息 - 使用队列异步处理"""
        msg_count = 0
        
        try:
            while self.running:
                try:
                    data = await ws.recv()
                    msg_count += 1
                    
                    if isinstance(data, str):
                        continue
                    
                    messages = self._unpack_data(data)
                    
                    # 将消息放入队列，而不是直接处理
                    for msg in messages:
                        try:
                            # 非阻塞放入队列
                            self.msg_queue.put_nowait(msg)
                        except asyncio.QueueFull:
                            logger.warning(f"[房间{self.room_id}] 消息队列已满，丢弃消息")
                    
                except websockets.exceptions.ConnectionClosed:
                    logger.debug(f"WebSocket 连接已关闭")
                    break
                except Exception as e:
                    logger.debug(f"接收消息异常：{e}")
                    break
        finally:
            logger.debug(f"监听结束，共接收 {msg_count} 条消息")
    
    async def _process_queue(self):
        """独立的消息处理协程 - 从队列中取消息并处理"""
        logger.info(f"[房间{self.room_id}] 消息处理协程已启动")
        processed_count = 0
        while self.running:
            try:
                # 从队列中取消息
                msg = await asyncio.wait_for(self.msg_queue.get(), timeout=1.0)
                processed_count += 1
                
                await self._handle_message(msg)
                self.msg_queue.task_done()
            except asyncio.TimeoutError:
                # 队列为空，继续等待
                continue
            except Exception as e:
                logger.error(f"[房间{self.room_id}] 处理队列消息异常：{e}")
        logger.info(f"[房间{self.room_id}] 消息处理协程已停止，共处理 {processed_count} 条消息")
    
    async def _handle_message(self, msg: Dict[str, Any]):
        """处理消息"""
        cmd = msg.get("cmd", "")
        
        # 弹幕消息 (cmd 可能是 "DANMU_MSG" 或 "DANMU_MSG:4:0:2:2:2:0" 等格式)
        if cmd.startswith("DANMU_MSG"):
            # ===== 新增：消息去重 =====
            msg_id = msg.get("dm_v2", "")
            if self._is_duplicate_msg(msg_id):
                logger.debug(f"跳过重复弹幕：{msg_id}")
                return
            
            info = msg.get("info", [])
            if len(info) >= 3:
                danmaku_data = {
                    "type": "danmaku",
                    "msg_id": msg_id,  # 弹幕 ID
                    "content": info[1],  # 弹幕内容
                    "timestamp": info[0][4],  # 发送时间
                    "user": {
                        "uid": info[2][0],  # 用户 ID
                        "name": info[2][1],  # 用户名
                        "is_admin": info[2][2] == 1,  # 是否房管
                        "is_vip": info[2][3] == 1,  # 是否 VIP
                        "guard_level": info[7] if len(info) > 7 else 0,  # 舰队等级
                    },
                    "medal": info[3] if len(info) > 3 and info[3] else None,  # 粉丝牌
                    "room_id": self.room_id,
                }
                if self.on_danmaku_callback:
                    await self.on_danmaku_callback(danmaku_data)
        
        # 礼物消息
        elif cmd == "SEND_GIFT":
            data = msg.get("data", {})
            gift_data = {
                "type": "gift",
                "user": {
                    "uid": data.get("uid"),
                    "name": data.get("uname"),
                },
                "gift_name": data.get("giftName"),
                "num": data.get("num"),
                "price": data.get("price"),
                "timestamp": data.get("timestamp"),
            }
            if self.on_danmaku_callback:
                await self.on_danmaku_callback(gift_data)
        
        # 醒目留言（Super Chat）
        elif cmd in ("SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"):
            data = msg.get("data", {})
            sc_data = {
                "type": "super_chat",
                "user": {
                    "uid": data.get("uid"),
                    "name": data.get("user_info", {}).get("uname"),
                    "face": data.get("user_info", {}).get("face"),
                },
                "message": data.get("message", ""),
                "price": data.get("price", 0),  # 价格（元）
                "time": data.get("time", 0),  # 持续时间（秒）
                "start_time": data.get("start_time"),  # 开始时间戳
                "end_time": data.get("end_time"),  # 结束时间戳
                "background_color": data.get("background_color"),  # 背景颜色
                "font_color": data.get("font_color"),  # 字体颜色
                "id": data.get("id"),  # SC ID
                "room_id": self.room_id,
            }
            logger.info(f"[房间{self.room_id}] 收到醒目留言: {sc_data['user']['name']} ￥{sc_data['price']}: {sc_data['message'][:30]}...")
            if self.on_danmaku_callback:
                await self.on_danmaku_callback(sc_data)
        
        # 进入直播间
        elif cmd == "INTERACT_WORD":
            data = msg.get("data", {})
            enter_data = {
                "type": "enter",
                "user": {
                    "uid": data.get("uid"),
                    "name": data.get("uname"),
                },
                "timestamp": data.get("timestamp"),
            }
            if self.on_danmaku_callback:
                await self.on_danmaku_callback(enter_data)
        
        # 其他消息类型可以根据需要添加
    
    async def start(self) -> bool:
        """启动客户端 - 同时连接多个服务器
        返回: True=启动成功, False=启动失败（包括致命错误）
        """
        if not await self.init_room():
            return False
        
        self.running = True
        self._fatal_error = None  # 重置致命错误
        self._server_status = {}  # 重置服务器状态
        self._tasks = []
        
        # ===== 新增：启动消息处理协程 =====
        self._queue_task = asyncio.create_task(self._process_queue())
        self._tasks.append(self._queue_task)
        
        # 同时连接所有服务器（最多 3 个）
        server_count = min(3, len(self.host_list))
        for i, host in enumerate(self.host_list[:server_count]):
            ws_url = f"wss://{host['host']}:{host['wss_port']}/sub"
            task = asyncio.create_task(self._connect_server(ws_url, i))
            self._tasks.append(task)
        
        # 等待连接结果，最多等待 8 秒
        max_wait_time = 8
        check_interval = 0.5
        waited_time = 0
        
        while waited_time < max_wait_time:
            await asyncio.sleep(check_interval)
            waited_time += check_interval
            
            # 检查是否有致命错误（如认证失败）
            if self._fatal_error:
                logger.error(f"启动失败：{self._fatal_error}")
                await self.stop()
                return False
            
            # 检查当前连接数
            connected = len([ws for ws in self.ws_list if ws])
            if connected > 0:
                # 至少有一个连接成功
                logger.info(f"弹幕客户端启动成功：room={self.room_id}, 连接数={connected}/{server_count}")
                return True
            
            # 检查是否所有服务器都已失败（非致命错误导致的重连耗尽）
            failed_servers = sum(1 for s in self._server_status.values() if s.get("status") == "failed")
            if failed_servers >= server_count:
                logger.error(f"所有弹幕服务器连接失败 ({failed_servers}/{server_count})")
                await self.stop()
                return False
        
        # 超时，检查最终状态
        connected = len([ws for ws in self.ws_list if ws])
        if connected == 0:
            logger.error(f"启动超时，未能建立任何连接")
            await self.stop()
            return False
        
        logger.info(f"弹幕客户端启动成功：room={self.room_id}, 连接数={connected}/{server_count}")
        return True
    
    async def _connect_server(self, ws_url: str, index: int):
        """连接单个服务器 - 带指数退避重连，但有最大重连次数限制"""
        reconnect_delay = 1  # 初始重连延迟（秒）
        max_delay = 30  # 最大重连延迟（秒）- 减少以更快失败
        reconnect_attempts = 0
        ws = None
        
        self._server_status[index] = {"status": "connecting", "error": None}
        
        while self.running:
            # 检查是否已达到最大重连次数
            if reconnect_attempts >= self._max_reconnect_per_server:
                logger.error(f"[连接{index+1}] 达到最大重连次数({self._max_reconnect_per_server})，停止重连")
                self._server_status[index] = {"status": "failed", "error": "max_retries_exceeded"}
                break
            
            # 检查是否有致命错误（认证失败等）
            if self._fatal_error:
                logger.error(f"[连接{index+1}] 检测到致命错误，停止重连: {self._fatal_error}")
                self._server_status[index] = {"status": "failed", "error": f"fatal: {self._fatal_error}"}
                break
            
            try:
                logger.info(f"[连接{index+1}] 正在连接：{ws_url}")
                ws = await websockets.connect(
                    ws_url,
                    ping_interval=None,  # 我们自己发心跳
                    max_size=None,  # 不限制消息大小
                    compression=None  # 禁用 WebSocket 压缩，我们已经手动解压
                )
                self.ws_list.append(ws)
                logger.info(f"[连接{index+1}] WebSocket 已连接")
                
                # 发送认证
                auth_success = await self._send_auth(ws)
                if not auth_success:
                    # 认证失败是致命错误，不应该重连
                    error_msg = f"[连接{index+1}] 认证失败，房间可能不存在或需要密码"
                    logger.error(error_msg)
                    self._fatal_error = "认证失败，请检查房间号是否正确"
                    self._server_status[index] = {"status": "failed", "error": "auth_failed"}
                    
                    # 清理资源
                    if ws in self.ws_list:
                        self.ws_list.remove(ws)
                    try:
                        await ws.close()
                    except:
                        pass
                    break  # 退出重连循环，不再尝试
                
                logger.info(f"[连接{index+1}] 认证成功")
                self._server_status[index] = {"status": "connected", "error": None}
                
                # 重置重连计数（成功连接后重置）
                reconnect_attempts = 0
                reconnect_delay = 1
                
                # 启动心跳和监听
                heartbeat_task = asyncio.create_task(self._send_heartbeat(ws))
                listen_task = asyncio.create_task(self._listen(ws))
                
                self._tasks.extend([heartbeat_task, listen_task])
                
                # 等待任务结束（连接断开时会返回）
                await asyncio.gather(heartbeat_task, listen_task, return_exceptions=True)
                
                # 连接断开，从列表中移除
                if ws in self.ws_list:
                    self.ws_list.remove(ws)
                logger.warning(f"[连接{index+1}] 连接断开")
                self._server_status[index] = {"status": "disconnected", "error": None}
                
            except websockets.exceptions.ConnectionClosed as e:
                # 连接正常关闭或异常关闭，记录日志后重连
                reconnect_attempts += 1
                if ws and ws in self.ws_list:
                    self.ws_list.remove(ws)
                if e.code == 1000 or e.code == 1001:
                    logger.info(f"[连接{index+1}] 连接正常关闭 (code={e.code})")
                else:
                    logger.warning(f"[连接{index+1}] 连接异常关闭 (code={e.code}, reason={e.reason})")
                
            except Exception as e:
                reconnect_attempts += 1
                if ws and ws in self.ws_list:
                    self.ws_list.remove(ws)
                
                # 根据错误类型记录不同级别的日志
                error_msg = str(e)
                if "no close frame" in error_msg:
                    logger.warning(f"[连接{index+1}] 连接意外断开")
                elif "Connection reset" in error_msg or "Connection aborted" in error_msg:
                    logger.warning(f"[连接{index+1}] 连接被重置")
                else:
                    logger.error(f"[连接{index+1}] 连接异常：{e}")
                
                # 尝试优雅关闭
                if ws:
                    try:
                        await ws.close()
                    except:
                        pass
                
            if not self.running:
                break
            
            # 指数退避重连
            delay = min(reconnect_delay * (1.5 ** (reconnect_attempts - 1)), max_delay)
            logger.info(f"[连接{index+1}] {delay:.1f}秒后重试 ({reconnect_attempts}/{self._max_reconnect_per_server})...")
            await asyncio.sleep(delay)
        
        # 连接彻底失败，记录最终状态
        if self._server_status.get(index, {}).get("status") not in ["connected"]:
            logger.error(f"[连接{index+1}] 连接彻底失败，不再重连")
    
    async def stop(self):
        """停止客户端 - 关闭所有连接并清理状态"""
        logger.info(f"正在停止弹幕客户端：room={self.room_id}")
        self.running = False
        
        # 取消所有任务
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # 关闭所有 WebSocket
        for ws in self.ws_list:
            try:
                await ws.close()
            except:
                pass
        
        # 清理所有状态
        self.ws_list.clear()
        self._tasks.clear()
        self._server_status.clear()
        self._fatal_error = None
        
        # 清空队列
        while not self.msg_queue.empty():
            try:
                self.msg_queue.get_nowait()
                self.msg_queue.task_done()
            except:
                pass
        
        logger.info(f"弹幕客户端已停止：room={self.room_id}")
