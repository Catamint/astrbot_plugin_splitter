import re
import math
import random
import asyncio
from typing import List

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import LLMResponse
from astrbot.api.message_components import Plain, BaseMessageComponent

class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pair_map = {
            '“': '”', '《': '》', '（': '）', '(': ')', 
            '[': ']', '{': '}', '"': '"', "'": "'"
        }

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        setattr(event, "__is_llm_reply", True)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        # 1. 校验逻辑
        if not getattr(event, "__is_llm_reply", False):
            return
        if getattr(event, "__splitter_processed", False):
            return
        setattr(event, "__splitter_processed", True)

        result = event.get_result()
        if not result or not result.chain:
            return

        # 2. 获取配置
        split_pattern = self.config.get("split_regex", r"[。？！?!\\n…]+")
        clean_pattern = self.config.get("clean_regex", "")
        smart_mode = self.config.get("enable_smart_split", True)
        max_segs = self.config.get("max_segments", 7)

        # 3. 执行分段
        segments = self.split_chain_smart(result.chain, split_pattern, smart_mode)

        # 4. 最大分段数限制逻辑
        # 如果分段数超过限制，将超出的部分全部合并到第 max_segs 段中
        if len(segments) > max_segs and max_segs > 0:
            logger.warning(f"[Splitter] 分段数({len(segments)}) 超过限制({max_segs})，正在合并剩余段落。")
            merged_last_segment = []
            # 保留前 max_segs - 1 段
            trimmed_segments = segments[:max_segs-1]
            # 合并剩余所有段
            for seg in segments[max_segs-1:]:
                merged_last_segment.extend(seg)
            
            trimmed_segments.append(merged_last_segment)
            segments = trimmed_segments

        # 如果只有一段，且不需要清理，则直接放行
        if len(segments) <= 1 and not clean_pattern:
            return

        logger.info(f"[Splitter] 将发送 {len(segments)} 个分段。")

        # 5. 逐段处理与发送
        for i, segment_chain in enumerate(segments):
            if not segment_chain:
                continue

            # 应用清理正则 (在发送前清理)
            if clean_pattern:
                for comp in segment_chain:
                    if isinstance(comp, Plain) and comp.text:
                        # 替换掉匹配的内容
                        comp.text = re.sub(clean_pattern, "", comp.text)

            # 提取纯文本用于日志和延迟计算
            text_content = "".join([c.text for c in segment_chain if isinstance(c, Plain)])
            
            # 如果清理后文本为空（且只有文本组件），则跳过发送
            is_only_text = all(isinstance(c, Plain) for c in segment_chain)
            if is_only_text and not text_content:
                continue

            logger.info(f"[Splitter] 发送第 {i+1}/{len(segments)} 段: 已分段文本：{text_content}")

            try:
                mc = MessageChain()
                mc.chain = segment_chain
                await self.context.send_message(event.unified_msg_origin, mc)

                # 延迟逻辑
                if i < len(segments) - 1:
                    wait_time = self.calculate_delay(text_content)
                    await asyncio.sleep(wait_time)

            except Exception as e:
                logger.error(f"[Splitter] 发送分段失败: {e}")

        # 6. 清空原始链
        result.chain.clear()

    def calculate_delay(self, text: str) -> float:
        """根据策略计算延迟时间"""
        strategy = self.config.get("delay_strategy", "log")
        
        if strategy == "random":
            mn = self.config.get("random_min", 1.0)
            mx = self.config.get("random_max", 3.0)
            return random.uniform(mn, mx)
            
        elif strategy == "log":
            base = self.config.get("log_base", 0.5)
            factor = self.config.get("log_factor", 0.8)
            length = len(text)
            delay = base + factor * math.log(length + 1)
            return min(delay, 5.0) 
            
        else: # fixed
            return self.config.get("fixed_delay", 1.5)

    def split_chain_smart(self, chain: List[BaseMessageComponent], pattern: str, smart_mode: bool) -> List[List[BaseMessageComponent]]:
        segments = []
        current_chain_buffer = []

        for component in chain:
            if not isinstance(component, Plain):
                current_chain_buffer.append(component)
                continue

            text = component.text
            if not text:
                continue

            if not smart_mode:
                self._process_text_simple(text, pattern, segments, current_chain_buffer)
            else:
                self._process_text_smart(text, pattern, segments, current_chain_buffer)

        if current_chain_buffer:
            segments.append(current_chain_buffer)

        return [seg for seg in segments if seg]

    def _process_text_simple(self, text: str, pattern: str, segments: list, buffer: list):
        parts = re.split(f"({pattern})", text)
        temp_text = ""
        for part in parts:
            if not part: continue
            if re.fullmatch(pattern, part):
                temp_text += part
                buffer.append(Plain(temp_text))
                segments.append(buffer[:])
                buffer.clear()
                temp_text = ""
            else:
                if temp_text: buffer.append(Plain(temp_text))
                temp_text = part
        if temp_text: buffer.append(Plain(temp_text))

    def _process_text_smart(self, text: str, pattern: str, segments: list, buffer: list):
        stack = []
        compiled_pattern = re.compile(pattern)
        i = 0
        n = len(text)
        current_chunk = ""

        while i < n:
            char = text[i]
            is_opener = char in self.pair_map
            
            # 处理引号特殊情况
            if char in ['"', "'"]:
                if stack and stack[-1] == char:
                    stack.pop()
                    current_chunk += char
                    i += 1
                    continue
                else:
                    stack.append(char)
                    current_chunk += char
                    i += 1
                    continue
            
            if stack:
                expected_closer = self.pair_map.get(stack[-1])
                if char == expected_closer:
                    stack.pop()
                elif is_opener:
                    stack.append(char)
                current_chunk += char
                i += 1
                continue
            
            if is_opener:
                stack.append(char)
                current_chunk += char
                i += 1
                continue

            match = compiled_pattern.match(text, pos=i)
            if match:
                delimiter = match.group()
                current_chunk += delimiter
                buffer.append(Plain(current_chunk))
                segments.append(buffer[:])
                buffer.clear()
                current_chunk = ""
                i += len(delimiter)
            else:
                current_chunk += char
                i += 1

        if current_chunk:
            buffer.append(Plain(current_chunk))
