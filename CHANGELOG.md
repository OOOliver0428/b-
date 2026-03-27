# Changelog

## [1.2.0] - 2026-03-28

### Bug Fixes (Critical)
- **run.py**: 修复 `threading` 模块导入位置错误导致 `DEBUG=True` 时 `NameError` 的问题
- **multi_danmaku_ws.py**: 标记为废弃模块，添加弃用警告，委托给 DanmakuClient
- **敏感词同步**: 前端敏感词初始化改为从后端 API 加载，解决前后端敏感词不同步问题
- **.gitignore**: 移除对 `sensitive_words/` 的忽略，确保默认敏感词库可提交到仓库

### Bug Fixes (Medium)
- **delete_danmaku**: 实现删除弹幕 API 端点，明确 B站不支持单条删除的限制
- **WebSocket 重连**: 前端添加指数退避自动重连机制（最多 5 次重连）
- **get_ban_list**: 修复 `ps` 参数误用为页码的问题，新增 `pn` 参数用于页码
- **双重审核**: 前端自动审核改用后端敏感词列表，减少前后端逻辑不一致

### Improvements
- **CORS**: 限制为 localhost 来源，提升安全性
- **版本号**: 统一 `app/main.py` 和 `package_exe.py` 中的版本号为 1.2.0
- **依赖清理**: 移除 `requirements.txt` 中未使用的依赖（aiohttp, protobuf, python-socketio, aioredis, python-jose, requests）
- **用户列表**: 添加 500 条上限，防止长时间运行导致内存泄漏
- **安全提示**: `.env.example` 添加敏感凭证安全警告
- **代码清理**: 移除 routes.py 中重复的 `import asyncio`

### Skipped (风险过高)
- Cookie 加密存储：改动范围大，可能影响现有用户配置
- 前端构建化改造（Vite + TypeScript）：属于大版本变更，不在本次迭代范围
- API Token 认证：可能影响现有用户使用方式

## [1.1.0] - 2026-03-16
- 多服务器弹幕连接
- 消息去重和队列缓冲
- WBI 签名认证
- Super Chat 支持

## [1.0.0] - 2026-03-01
- 初始版本发布
- 基础弹幕监控
- 房管禁言/解禁功能
- 敏感词过滤
