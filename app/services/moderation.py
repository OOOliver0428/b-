"""弹幕审核服务"""
import os
import re
from typing import List, Dict, Optional, Callable
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from loguru import logger

from app.core.config import settings, get_external_path


class ActionType(Enum):
    """处理动作类型"""
    PASS = "pass"           # 通过
    BLOCK = "block"         # 屏蔽
    BAN = "ban"             # 禁言
    DELETE = "delete"       # 删除


@dataclass
class ModerationResult:
    """审核结果"""
    action: ActionType
    reason: str
    duration: int = 0  # 禁言时长（小时）


class ModerationService:
    """弹幕审核服务"""
    
    def __init__(self):
        self.sensitive_words: List[str] = []
        self.regex_patterns: List[re.Pattern] = []
        self.rules: List[Callable] = []
        # 敏感词触发统计
        self.trigger_stats: Counter = Counter()
        # 当前加载的文件名
        self.loaded_files: List[str] = []
        # 敏感词文件目录
        self._words_dir = os.path.join(get_external_path(), "sensitive_words")
        
        self._load_default_rules()
        self._load_default_words_on_startup()
    
    def _load_default_words_on_startup(self):
        """启动时自动加载默认敏感词库"""
        default_file = os.path.join(self._words_dir, "default.md")
        if os.path.exists(default_file):
            words = self._read_words_file(default_file)
            self.sensitive_words = words
            self.loaded_files = ["default.md"]
            logger.info(f"启动时自动加载 default.md: {len(words)} 个敏感词")
        else:
            # 回退到 .env 配置
            words = settings.sensitive_words_list
            self.sensitive_words = words
            logger.info(f"从 .env 加载了 {len(words)} 个敏感词")
    
    def _read_words_file(self, filepath: str) -> List[str]:
        """读取单个敏感词文件"""
        words = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    words.append(line)
        except Exception as e:
            logger.error(f"读取敏感词文件失败 {filepath}: {e}")
        return words
    
    def _write_words_file(self, filename: str, words: List[str]) -> bool:
        """写入敏感词文件"""
        filepath = os.path.join(self._words_dir, filename)
        try:
            os.makedirs(self._words_dir, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"# 敏感词列表 - {filename}\n")
                f.write("# 每行一个词，# 开头的行为注释\n\n")
                for word in words:
                    f.write(f"{word}\n")
            logger.info(f"已写入敏感词文件 {filename}: {len(words)} 个词")
            return True
        except Exception as e:
            logger.error(f"写入敏感词文件失败 {filepath}: {e}")
            return False
    
    def _load_default_rules(self):
        """加载默认审核规则"""
        # 规则1: 敏感词检测
        self.rules.append(self._check_sensitive_words)
        
        # 规则2: 重复字符检测（刷屏）
        self.rules.append(self._check_spam)
        
        # 规则3: 广告检测
        self.rules.append(self._check_advertisement)
    
    def add_sensitive_word(self, word: str, persist_file: str = "default.md") -> bool:
        """添加敏感词并持久化到文件"""
        if not word or word in self.sensitive_words:
            return False
        self.sensitive_words.append(word)
        # 持久化
        return self._write_words_file(persist_file, self.sensitive_words)
    
    def remove_sensitive_word(self, word: str, persist_file: str = "default.md") -> bool:
        """移除敏感词并持久化到文件"""
        if word not in self.sensitive_words:
            return False
        self.sensitive_words.remove(word)
        # 持久化
        return self._write_words_file(persist_file, self.sensitive_words)
    
    def load_file(self, filename: str) -> int:
        """加载指定敏感词文件（替换当前列表）"""
        filepath = os.path.join(self._words_dir, filename)
        if not os.path.exists(filepath):
            logger.warning(f"敏感词文件不存在: {filepath}")
            return 0
        words = self._read_words_file(filepath)
        self.sensitive_words = words
        if filename not in self.loaded_files:
            self.loaded_files.append(filename)
        logger.info(f"已加载敏感词文件 {filename}: {len(words)} 个词")
        return len(words)
    
    def load_file_merge(self, filename: str) -> int:
        """加载指定敏感词文件（合并到当前列表）"""
        filepath = os.path.join(self._words_dir, filename)
        if not os.path.exists(filepath):
            return 0
        words = self._read_words_file(filepath)
        added = 0
        for w in words:
            if w not in self.sensitive_words:
                self.sensitive_words.append(w)
                added += 1
        if filename not in self.loaded_files:
            self.loaded_files.append(filename)
        logger.info(f"合并加载敏感词文件 {filename}: 新增 {added} 个词")
        return added
    
    def _check_sensitive_words(self, danmaku: Dict) -> Optional[ModerationResult]:
        """检测敏感词"""
        content = danmaku.get("content", "")
        
        for word in self.sensitive_words:
            if word in content:
                return ModerationResult(
                    action=ActionType.BAN,
                    reason=f"包含敏感词: {word}",
                    duration=1  # 禁言1小时
                )
        return None
    
    def _check_spam(self, danmaku: Dict) -> Optional[ModerationResult]:
        """检测刷屏（重复字符）"""
        content = danmaku.get("content", "")
        
        # 检测重复字符超过10个
        for char in set(content):
            if content.count(char) > 10:
                return ModerationResult(
                    action=ActionType.BLOCK,
                    reason="刷屏/重复字符过多"
                )
        
        # 检测重复字符串
        if len(content) >= 6:
            for i in range(2, len(content) // 2):
                pattern = content[:i]
                if content == pattern * (len(content) // i) + pattern[:len(content) % i]:
                    return ModerationResult(
                        action=ActionType.BLOCK,
                        reason="刷屏/重复内容"
                    )
        
        return None
    
    def _check_advertisement(self, danmaku: Dict) -> Optional[ModerationResult]:
        """检测广告"""
        content = danmaku.get("content", "")
        
        # 广告关键词
        ad_keywords = ["加群", "qq群", "QQ群", "VX", "微信", "vx:", "微信:", 
                      " QQ", "qq:", "扫码", "二维码", "优惠券", "低价出", "出号"]
        
        # 检测联系方式
        patterns = [
            r"[\u4e00-\u9fa5]*[0-9a-zA-Z]{5,}@(?:qq|163|126|gmail)\.com",  # 邮箱
            r"(?:加|联系).*?(?:微|V|v|Q|q).*?(?:信|Q|q).*?(?:[:：]|是).*?\d+",  # 联系方式
            r"[\u4e00-\u9fa5]{0,3}[:：]\s*[a-zA-Z0-9]{6,}",  # 可能是微信号/QQ号
        ]
        
        for keyword in ad_keywords:
            if keyword in content:
                return ModerationResult(
                    action=ActionType.BAN,
                    reason=f"疑似广告: 包含 '{keyword}'",
                    duration=24  # 禁言24小时
                )
        
        for pattern in patterns:
            if re.search(pattern, content):
                return ModerationResult(
                    action=ActionType.BAN,
                    reason="疑似广告联系方式",
                    duration=24
                )
        
        return None
    
    async def check(self, danmaku: Dict) -> ModerationResult:
        """
        审核弹幕
        返回审核结果
        """
        for rule in self.rules:
            result = rule(danmaku)
            if result:
                content = danmaku.get("content", "") or danmaku.get("message", "")
                logger.info(f"弹幕审核不通过: {result.reason}, 内容: {content}")
                # 记录触发统计
                if result.action != ActionType.PASS:
                    self.trigger_stats[result.reason] += 1
                return result
        
        return ModerationResult(action=ActionType.PASS, reason="")
    
    def get_stats(self) -> Dict:
        """获取审核服务统计"""
        return {
            "sensitive_words_count": len(self.sensitive_words),
            "rules_count": len(self.rules),
            "loaded_files": self.loaded_files,
            "trigger_stats": dict(self.trigger_stats.most_common(20)),
        }


# 全局实例
moderation_service = ModerationService()
