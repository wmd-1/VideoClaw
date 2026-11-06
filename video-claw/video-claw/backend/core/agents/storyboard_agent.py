# -*- coding: utf-8 -*-
"""
阶段3: 分镜智能体
基于剧本JSON，逐场景拆分为带时长标签的分镜（shots），按幕分组输出。
支持 Segment -> Shots 嵌套结构。
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from .base_agent import AgentInterface, heartbeat_ascent

logger = logging.getLogger(__name__)

# 分镜阶段的进度口径（openspec 之外的展示层语义，单一进度源见 _StoryboardProgress）
STORYBOARD_PHASE = "分镜设计"
HEARTBEAT_INTERVAL_SECONDS = 8.0     # 长耗时模型调用期间的心跳间隔
PROGRESS_MIN_EVENT_INTERVAL = 4.0    # 心跳事件节流窗口：并发多集时不让 tick 放大成事件洪水


def _get_storyboard_prompt(name: str, lang: str = "zh") -> str:
    from prompts.loader import load_prompt_with_fallback
    return load_prompt_with_fallback("storyboard", name, lang, "zh")


class _StoryboardProgress:
    """分镜阶段的唯一进度源：工作量加权 + 单调夹逼 + 运行期整数封顶 95 + 心跳节流。

    记账单位 = 一次模型调用步（片段规划 1、每个片段设计 1、站位连续性检查 1）。
    权重按「增量式」登记：先用纯文本拆分路径估算每集片段数把分母做准，规划返回
    真实片段数后再补齐差值；分母只增不减，配合 percent 单调夹逼，任何时刻都不倒退。

    percent 只在这里计算并发送，调用方 MUST NOT 自行换算档位（原「按已完成集数推进」
    的第二进度源就是进度条钉在 10% 的根因：所有集并发跑，完成时刻挤在末尾）。
    """

    START_PERCENT = 5      # 阶段启动第一报（沿用原值）
    FLOOR_PERCENT = 10     # 运行期下限（沿用原值）
    RUN_CAP = 95           # 运行期整数封顶，最后的 100 只由 finish 报
    RUN_SPAN = RUN_CAP - FLOOR_PERCENT

    def __init__(self, report: Callable[..., None], *, min_interval: float = 0.0,
                 clock: Callable[[], float] = time.monotonic):
        """`report(message, percent, data=None)` 由调用方接入（阶段名在接入点给定）。"""
        self._report = report
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self.done = 0
        self.total = 0
        self.percent = self.START_PERCENT
        self._last_tick_at: Optional[float] = None
        self._last_message: Optional[str] = None

    # ─────────── 记账 ───────────

    def add_total(self, count: int) -> None:
        """增量登记新发现的模型调用步；分母只增不减（估算偏高时不回退 percent）。"""
        try:
            growth = int(count)
        except (TypeError, ValueError):
            return
        if growth > 0:
            self.total += growth

    def complete(self, count: int = 1, message: str = "", data: Optional[dict] = None) -> int:
        """登记已完成的调用步；带消息时按新档位立即上报（里程碑不节流）。"""
        try:
            moved = int(count)
        except (TypeError, ValueError):
            moved = 0
        if moved > 0:
            self.done = min(self.total, self.done + moved)
        if message:
            self._emit(message, self._target(), data=data)
        return self.percent

    # ─────────── 上报 ───────────

    def begin(self, message: str) -> int:
        self._emit(message, self.START_PERCENT)
        return self.percent

    def note(self, message: str, data: Optional[dict] = None) -> int:
        return self._emit(message, self._target(), data=data)

    def finish(self, message: str = "完成") -> int:
        return self._emit(message, 100)

    def beat(self, message: str, suggested: Optional[float] = None) -> bool:
        """心跳上报：把建议档位落到自己的量程内；无可推进量或落在节流窗口内则不发。"""
        ceiling = max(self._band(), self._next_band())
        if suggested is None:
            target = heartbeat_ascent(float(self.percent), float(ceiling))
        else:
            target = float(suggested)
        target = min(max(target, float(self.percent)), float(ceiling), float(self.RUN_CAP))
        if int(target) <= self.percent:
            return False
        now = self._clock()
        if self._min_interval > 0 and self._last_tick_at is not None \
                and now - self._last_tick_at < self._min_interval:
            return False
        self._last_tick_at = now
        self._emit(message, int(target))
        return True

    def heartbeat_window(self) -> Tuple[float, float]:
        """给 `_await_with_heartbeat` 的量程：当前档位 → 下一步完成应到的档位。"""
        ceiling = max(self._band(), self._next_band())
        return float(self.percent), float(max(self.percent, ceiling))

    # ─────────── 内部 ───────────

    def _band(self) -> int:
        if self.total <= 0:
            return self.FLOOR_PERCENT
        return min(self.RUN_CAP, self.FLOOR_PERCENT + int(self.RUN_SPAN * self.done / self.total))

    def _next_band(self) -> int:
        """当前等待中的那一步完成后应到的档位：心跳 MUST NOT 越过未完成的工作。"""
        if self.total <= 0:
            return self.FLOOR_PERCENT
        pending = min(self.done + 1, self.total)
        return min(self.RUN_CAP, self.FLOOR_PERCENT + int(self.RUN_SPAN * pending / self.total))

    def _target(self) -> int:
        return max(self.FLOOR_PERCENT, self._band())

    def _emit(self, message: str, percent: int, data: Optional[dict] = None) -> int:
        # 单一进度源的三条不变量：整数、只前进、运行期不越过 95（100 只来自 finish）。
        resolved = int(max(self.percent, min(100, int(percent))))
        if resolved > self.RUN_CAP and 100 != resolved:
            resolved = self.RUN_CAP
        if data is None and resolved == self.percent and message == self._last_message:
            return self.percent
        self.percent = resolved
        self._last_message = message
        self._report(message, resolved, data)
        return resolved


class StoryboardAgent(AgentInterface):
    def __init__(self):
        super().__init__(name="Storyboard")

    MIN_SHOT_DURATION = 2
    MIN_UNIT_DURATION = 2
    MAX_UNIT_DURATION = 15
    MIN_SEGMENT_DURATION = 5
    MAX_SEGMENT_DURATION = 15
    OPENING_SHOT_TYPES = {"中景", "全景"}

    @staticmethod
    def _extract_json_array(text: str) -> Optional[List[dict]]:
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group())
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _extract_json_object(text: str) -> Optional[dict]:
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end + 1])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _clean_script_line(line: str) -> str:
        line = line.strip()
        line = re.sub(r"^[-*•]\s*", "", line)
        line = re.sub(r"^<action>\s*", "", line, flags=re.I)
        line = re.sub(r"\s*</action>$", "", line, flags=re.I)
        line = re.sub(r"\s+", " ", line)
        return line.strip()

    @staticmethod
    def _strip_markup(text: str) -> str:
        text = re.sub(r"^[#>\s]+", "", text.strip())
        text = text.strip("*_` \t")
        return text.strip()

    @staticmethod
    def _character_names(characters: List[dict]) -> List[str]:
        names = []
        for item in characters:
            name = str(item.get("name", "")).strip()
            if name:
                names.append(name)
        return sorted(set(names), key=len, reverse=True)

    @staticmethod
    def _setting_names(settings: List[dict]) -> List[str]:
        names = []
        for item in settings:
            name = str(item.get("name", "")).strip()
            if name:
                names.append(name)
            # 导入产物可能带别名（同一地点的其他叫法）
            for alias in item.get("aliases") or []:
                text = str(alias).strip()
                if text:
                    names.append(text)
        return sorted(set(names), key=len, reverse=True)

    @classmethod
    def _match_characters(cls, text: str, character_names: List[str]) -> List[str]:
        """Only use the existing character list as keywords; never invent names."""
        return [name for name in character_names if name and name in text]

    @staticmethod
    def _setting_matches_text(setting_name: str, text: str) -> bool:
        if setting_name in text:
            return True
        normalized = re.sub(r"[（）()【】\[\]\s]", "", setting_name)
        text_normalized = re.sub(r"\s", "", text)
        if normalized and normalized in text_normalized:
            return True
        base_name = re.sub(r"[（(].*?[）)]", "", setting_name).strip()
        return bool(base_name and base_name in text)

    @classmethod
    def _resolve_location(
        cls,
        text: str,
        current_location: str,
        setting_names: List[str],
        alias_map: Optional[Dict[str, str]] = None,
    ) -> str:
        mapping = alias_map or {}
        for name in setting_names:
            if cls._setting_matches_text(name, text):
                # 别名只用于匹配，落盘地点必须回规范名，否则下游取不到场景参考图
                return mapping.get(name, name)
        if current_location:
            return current_location
        fallback = setting_names[0] if setting_names else ""
        return mapping.get(fallback, fallback)

    @staticmethod
    def _alias_map(settings: List[dict]) -> Dict[str, str]:
        """别名 -> 规范场景名；规范名自身也映射到自己。"""
        mapping: Dict[str, str] = {}
        for item in settings:
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("name", "")).strip()
            if not canonical:
                continue
            mapping[canonical] = canonical
            for alias in item.get("aliases") or []:
                text = str(alias).strip()
                if text:
                    mapping[text] = canonical
        return mapping

    @staticmethod
    def _canonical_location(location: str, alias_map: Dict[str, str]) -> str:
        text = str(location or "").strip()
        return alias_map.get(text, text)

    @classmethod
    def _parse_scene_header_parts(cls, line: str, setting_names: List[str]) -> Optional[dict]:
        clean = cls._strip_markup(line)
        if not clean:
            return None

        # Examples:
        # **第1集-第1场 日 内 高二三班教室**
        # **1-1 夜 内 锐创科技办公室**
        header_patterns = [
            r"^第\d+集\s*[-—]\s*第\d+场\s+(日|夜|晨|傍晚|深夜)\s+(内|外)\s+(.+)$",
            r"^\d+\s*[-—_]\s*\d+\s+(日|夜|晨|傍晚|深夜)\s+(内|外)\s+(.+)$",
            # 英文场志：Episode 1 - Scene 1 Day INT Office / Episode 1, Scene 1 … / Scene 1 …
            r"^(?:Episode\s+\d+\s*[-—,]\s*)?Scene\s+\d+\s+(day|night|morning|evening|dawn|dusk)\s+(INT\.?|EXT\.?|INT\.?/EXT\.?)\s+(.+)$",
            # 简写：1-1 Day INT Office
            r"^\d+\s*[-—]\s*\d+\s+(day|night|morning|evening|dawn|dusk)\s+(INT\.?|EXT\.?)\s+(.+)$",
        ]
        english_time = {
            "day": "日", "night": "夜", "morning": "晨",
            "evening": "傍晚", "dawn": "晨", "dusk": "傍晚",
        }
        english_space = {
            "int": "内", "ext": "外", "int/ext": "内/外",
        }
        for pattern in header_patterns:
            match = re.match(pattern, clean, flags=re.I)
            if not match:
                continue
            time_of_day = match.group(1).strip()
            scene_space = match.group(2).strip()
            time_of_day = english_time.get(time_of_day.lower(), time_of_day)
            scene_space_key = scene_space.lower().replace(".", "")
            scene_space = english_space.get(scene_space_key, scene_space)
            candidate = match.group(3).strip()
            for name in setting_names:
                if cls._setting_matches_text(name, candidate):
                    return {
                        "location": name,
                        "scene_time": time_of_day,
                        "scene_space": scene_space,
                        "scene_context": clean,
                    }
            return {
                "location": candidate[:40] or clean[:40],
                "scene_time": time_of_day,
                "scene_space": scene_space,
                "scene_context": clean,
            }
        return None

    @classmethod
    def _parse_scene_header(cls, line: str, setting_names: List[str]) -> Optional[str]:
        parts = cls._parse_scene_header_parts(line, setting_names)
        if not parts:
            return None
        return parts.get("location")

    @staticmethod
    def _is_metadata_line(line: str) -> bool:
        return bool(re.match(
            r"^(?:剧本名称|时长|风格|类型|标题|Title|Duration|Genre|Style)\s*[:：]",
            line, flags=re.I,
        ))

    @staticmethod
    def _is_end_marker(line: str) -> bool:
        return bool(re.match(r"^[（(]?(?:第.+集\s*)?完[）)]?$|^\(?THE END\)?$", line.strip(), flags=re.I))

    @staticmethod
    def _dialogue_parts(line: str) -> Optional[Tuple[str, str, str]]:
        """Return speaker, tone/action, dialogue when a line looks like dialogue."""
        match = re.match(r"^([^:：]{1,24}?)(?:[（(]([^）)]{0,40})[）)])?[:：]\s*(.+)$", line)
        if not match:
            return None
        speaker = match.group(1).strip()
        tone = (match.group(2) or "").strip()
        dialogue = match.group(3).strip()
        if not dialogue:
            return None
        # Avoid treating section labels as dialogue.
        if speaker in {"人物", "场景", "画面", "镜头", "地点", "时间"}:
            return None
        return speaker, tone, dialogue

    @classmethod
    def _annotation_duration(
        cls, annotation: Optional[dict], text: str = "", is_dialogue: bool = False
    ) -> Optional[int]:
        """导入质检给出的行级建议时长优先；对话/旁白取与字数估算的较大者，避免压短。"""
        if not isinstance(annotation, dict):
            return None
        try:
            seconds = int(float(annotation.get("duration")))
        except (TypeError, ValueError, OverflowError):
            return None
        seconds = max(cls.MIN_UNIT_DURATION, min(cls.MAX_UNIT_DURATION, seconds))
        # 台词/旁白必须保证足够表达时间，时长只可上调不可因建议被压短；
        # 旁白传入的 is_dialogue 为 False，必须显式按字数估算，否则下限会退化成 2 秒
        if str(annotation.get("type") or "") in ("dialogue", "narration"):
            return max(seconds, cls._duration_for_script_unit(text, True))
        return seconds

    @classmethod
    def _duration_for_script_unit(cls, text: str, is_dialogue: bool) -> int:
        if not is_dialogue:
            return 2
        dialogue = cls._dialogue_parts(text)
        dialogue_text = dialogue[2] if dialogue else text
        visible_text = re.sub(r"\s+", "", dialogue_text)
        duration = (len(visible_text) + 4) // 5
        return max(2, min(15, duration))

    @classmethod
    def _is_entry_action(cls, text: str, character_names: List[str]) -> bool:
        if cls._dialogue_parts(text):
            return False
        if character_names and not cls._match_characters(text, character_names):
            return False
        return bool(re.search(
            r"(走进|走入|进入|进来|推门而入|推门进|冲进|闯进|踏进|来到|回到|走向(?:教室|办公室|会议室|厕所|房间|门口))",
            text,
        ))

    @classmethod
    def _annotate_episode_script(
        cls,
        script_text: str,
        characters: List[dict],
        settings: List[dict],
        annotations: Optional[List[dict]] = None,
        scene_spans: Optional[List[dict]] = None,
        line_offset: Optional[int] = None,
        baseline_line_count: Optional[int] = None,
    ) -> Tuple[str, List[dict]]:
        character_names = cls._character_names(characters)
        setting_names = cls._setting_names(settings)
        raw_lines = script_text.replace("\r\n", "\n").split("\n")

        alias_map = cls._alias_map(settings)
        # 画面描述行不单独成镜头：它的停留建议合并到同场景下一个动作/台词
        pending_scene_duration = 0

        # 导入标注是全局行号：只有标注未越界时整体启用，否则回退正则
        annotation_by_line: Dict[int, dict] = {}
        if isinstance(annotations, list) and annotations and line_offset is not None:
            try:
                max_line = max(
                    int(item.get("line", -1))
                    for item in annotations
                    if isinstance(item, dict)
                )
            except (TypeError, ValueError):
                max_line = -1
            if baseline_line_count is None or max_line < int(baseline_line_count):
                for item in annotations:
                    if not isinstance(item, dict):
                        continue
                    try:
                        annotation_by_line[int(item.get("line"))] = item
                    except (TypeError, ValueError):
                        continue
        use_annotations = bool(annotation_by_line)

        span_by_line: Dict[int, dict] = {}
        if use_annotations and isinstance(scene_spans, list):
            for span in scene_spans:
                if not isinstance(span, dict):
                    continue
                try:
                    from_line = int(span.get("from_line"))
                    to_line = int(span.get("to_line"))
                except (TypeError, ValueError):
                    continue
                for line_no in range(from_line, min(to_line, from_line + 2000) + 1):
                    span_by_line[line_no] = span

        annotated_lines: List[str] = []
        units: List[dict] = []
        current_location = cls._canonical_location(
            setting_names[0] if setting_names else "", alias_map
        )
        current_scene_time = ""
        current_scene_space = ""
        current_scene_context = ""
        scene_characters: List[str] = []
        scene_key = 0

        for line_number, raw_line in enumerate(raw_lines, 1):
            stripped = raw_line.strip()
            line = cls._clean_script_line(cls._strip_markup(stripped))
            if not line:
                continue
            global_line = (line_offset or 0) + (line_number - 1)
            annotation = annotation_by_line.get(global_line) if use_annotations else None
            if use_annotations:
                span = span_by_line.get(global_line)
                if span:
                    span_setting = str(span.get("setting") or "").strip()
                    if span_setting and span_setting != current_location:
                        current_location = span_setting
                        scene_key += 1
                        pending_scene_duration = 0
                        scene_characters = cls._match_characters(line, character_names)
            if annotation and annotation.get("type") in {
                "heading",
                "meta",
                "cast_block",
                "subtitle",
                "end_card",
            }:
                if annotation.get("type") == "heading":
                    # 场景标题即边界：上一场的画面描述停留不得泄漏到新场景
                    pending_scene_duration = 0
                    span = span_by_line.get(global_line) if use_annotations else None
                    # scene_spans 缺失或未覆盖该标题时，回退既有场景头解析
                    if not span:
                        header_parts = cls._parse_scene_header_parts(stripped, setting_names)
                        if header_parts:
                            scene_key += 1
                            current_location = cls._canonical_location(
                                header_parts.get("location", current_location), alias_map
                            )
                            current_scene_time = header_parts.get("scene_time", "")
                            current_scene_space = header_parts.get("scene_space", "")
                            current_scene_context = header_parts.get(
                                "scene_context", cls._strip_markup(stripped)
                            )
                            scene_characters = cls._match_characters(line, character_names)
                annotated_lines.append(line)
                continue
            if annotation and annotation.get("type") == "placeholder":
                duration = cls._annotation_duration(annotation) or 2
                unit_id = f"U{len(units) + 1:03d}"
                units.append(
                    {
                        "unit_id": unit_id,
                        "line_number": line_number,
                        "text": line,
                        "duration": duration,
                        "is_dialogue": False,
                        "is_narration": False,
                        "speaker": "",
                        "tone": "",
                        "characters": cls._match_characters(line, character_names),
                        "scene_characters": scene_characters[:],
                        "scene_key": scene_key,
                        "location": cls._resolve_location(
                            line, current_location, setting_names, alias_map
                        ),
                        "scene_time": current_scene_time,
                        "scene_space": current_scene_space,
                        "scene_context": current_scene_context,
                        "is_entry": False,
                    }
                )
                # 必须带时长与 U 编号，否则分段规划无法覆盖该单元
                annotated_lines.append(f"[{duration}秒][{unit_id}] {line}")
                continue
            if cls._is_metadata_line(line) or cls._is_end_marker(line):
                annotated_lines.append(line)
                continue

            header_parts = cls._parse_scene_header_parts(stripped, setting_names)
            if header_parts:
                scene_key += 1
                pending_scene_duration = 0
                current_location = cls._canonical_location(
                    header_parts.get("location", current_location), alias_map
                )
                current_scene_time = header_parts.get("scene_time", "")
                current_scene_space = header_parts.get("scene_space", "")
                current_scene_context = header_parts.get("scene_context", cls._strip_markup(stripped))
                scene_characters = cls._match_characters(line, character_names)
                annotated_lines.append(cls._strip_markup(stripped))
                continue

            if re.match(r"^(?:人物|Characters)\s*[:：]", line, flags=re.I):
                scene_characters = cls._match_characters(line, character_names)
                annotated_lines.append(line)
                continue

            dialogue = cls._dialogue_parts(line)
            is_narration = bool(annotation and annotation.get("type") == "narration") or bool(
                re.match(r"^(?:旁白|Narration|VO|V\.O\.)\s*[:：]", stripped, flags=re.I)
            )
            is_action = bool(re.match(r"^<action>", stripped, flags=re.I)) or bool(re.search(r"</action>$", stripped, flags=re.I))
            if annotation and annotation.get("type") == "dialogue":
                speaker_hint = str(annotation.get("speaker") or "").strip()
                if speaker_hint:
                    dialogue = (speaker_hint, str(annotation.get("tone") or ""), line)
                    is_action = False
            elif annotation and annotation.get("type") == "action":
                # 保真导入把普通动作正文标为 action，不能要求原文自带 <action> 标签
                is_action = True
            if is_narration:
                # 旁白不是对白：即使带冒号也不能进入 dialogue 分支
                dialogue = None
            if annotation and annotation.get("type") == "scene_desc":
                scene_desc_duration = cls._annotation_duration(annotation, line, False)
                if scene_desc_duration:
                    pending_scene_duration = max(pending_scene_duration, scene_desc_duration)
                annotated_lines.append(line)
                continue
            if not dialogue and not is_action and not is_narration:
                annotated_lines.append(line)
                continue

            unit_id = f"U{len(units) + 1:03d}"
            is_dialogue = dialogue is not None
            duration = cls._annotation_duration(annotation, line, is_dialogue)
            if duration is None:
                duration = cls._duration_for_script_unit(line, is_dialogue)
            if pending_scene_duration:
                duration = max(duration, pending_scene_duration)
                pending_scene_duration = 0
            # 旁白不自动创建可见人物（即使正文出现了人名）
            matched_chars = [] if is_narration else cls._match_characters(line, character_names)
            unit_chars = matched_chars[:]
            speaker = ""
            tone = ""
            if dialogue:
                speaker, tone, _ = dialogue
                speaker_matches = cls._match_characters(speaker, character_names)
                if speaker_matches:
                    unit_chars = list(dict.fromkeys(speaker_matches + unit_chars))

            units.append({
                "unit_id": unit_id,
                "line_number": line_number,
                "text": line,
                "duration": duration,
                "is_dialogue": is_dialogue,
                "is_narration": is_narration,
                "speaker": speaker,
                "tone": tone,
                "characters": unit_chars,
                "scene_characters": scene_characters[:],
                "scene_key": scene_key,
                "location": cls._resolve_location(
                    line, current_location, setting_names, alias_map
                ),
                "scene_time": current_scene_time,
                "scene_space": current_scene_space,
                "scene_context": current_scene_context,
                "is_entry": cls._is_entry_action(line, character_names),
            })
            annotated_lines.append(f"[{duration}秒][{unit_id}] {line}")

        return "\n".join(annotated_lines), units

    @classmethod
    def _segment_plan_from_units(cls, ep_n: int, segment_number: int, items: List[dict]) -> dict:
        characters: List[str] = []
        for item in items:
            for name in item.get("characters") or item.get("scene_characters") or []:
                if name not in characters:
                    characters.append(name)
        return {
            "episode_number": ep_n,
            "segment_number": segment_number,
            "location": items[0].get("location", "") if items else "",
            "scene_time": items[0].get("scene_time", "") if items else "",
            "scene_space": items[0].get("scene_space", "") if items else "",
            "scene_context": items[0].get("scene_context", "") if items else "",
            "characters": characters,
            "total_duration": sum(int(item.get("duration") or 0) for item in items),
            "items": items,
        }

    @classmethod
    def _extract_plan_unit_ids(cls, seg: dict) -> List[str]:
        ids: List[str] = []
        for value in seg.get("unit_ids") or []:
            if isinstance(value, str) and re.match(r"^U\d{3,}$", value):
                ids.append(value)
        for item in seg.get("items") or []:
            if isinstance(item, dict):
                unit_id = item.get("unit_id")
                if isinstance(unit_id, str) and re.match(r"^U\d{3,}$", unit_id):
                    ids.append(unit_id)
        if not ids:
            raw = json.dumps(seg, ensure_ascii=False)
            ids = re.findall(r"\bU\d{3,}\b", raw)
        return list(dict.fromkeys(ids))

    @classmethod
    def _validate_segment_plan(cls, ep_n: int, raw_plan: List[dict], units: List[dict]) -> List[dict]:
        unit_by_id = {item["unit_id"]: item for item in units}
        source_ids = [item["unit_id"] for item in units]
        flat_ids: List[str] = []
        plans: List[dict] = []

        for seg in raw_plan:
            if not isinstance(seg, dict):
                raise ValueError("片段规划包含非对象元素")
            ids = cls._extract_plan_unit_ids(seg)
            if not ids:
                raise ValueError("片段缺少 unit_ids")
            for unit_id in ids:
                if unit_id not in unit_by_id:
                    raise ValueError(f"片段包含未知 unit_id: {unit_id}")
                if unit_id in flat_ids:
                    raise ValueError(f"片段重复引用 unit_id: {unit_id}")

            items = [unit_by_id[unit_id] for unit_id in ids]
            scene_keys = {item.get("scene_key") for item in items}
            if len(scene_keys) > 1:
                raise ValueError("片段跨场景，场景切换必须分片段")
            for idx, item in enumerate(items):
                if item.get("is_entry") and idx != 0:
                    raise ValueError(f"{item['unit_id']} 是入场动作，必须作为片段开头")

            duration = sum(int(item.get("duration") or 0) for item in items)
            if duration > cls.MAX_SEGMENT_DURATION:
                raise ValueError(f"片段时长 {duration}s 超出上限 {cls.MAX_SEGMENT_DURATION}s")

            flat_ids.extend(ids)
            plans.append(cls._segment_plan_from_units(ep_n, len(plans) + 1, items))

        if flat_ids != source_ids:
            missing = [unit_id for unit_id in source_ids if unit_id not in flat_ids]
            extra = [unit_id for unit_id in flat_ids if unit_id not in source_ids]
            raise ValueError(f"片段规划动作/台词缺漏或乱序，missing={missing}, extra={extra}")
        return plans

    @classmethod
    def _fallback_segment_plan(cls, ep_n: int, units: List[dict]) -> List[dict]:
        plans: List[dict] = []
        current: List[dict] = []

        def flush_current():
            nonlocal current
            if not current:
                return
            plans.append(cls._segment_plan_from_units(ep_n, len(plans) + 1, current))
            current = []

        for unit in units:
            current_duration = sum(int(item.get("duration") or 0) for item in current)
            scene_changed = current and unit.get("scene_key") != current[-1].get("scene_key")
            entry_boundary = current and unit.get("is_entry")
            would_overflow = current and current_duration + int(unit.get("duration") or 0) > cls.MAX_SEGMENT_DURATION

            if scene_changed or entry_boundary or would_overflow:
                flush_current()
            current.append(unit)

        flush_current()
        return plans

    @staticmethod
    def _scene_item_payload(items: List[dict]) -> List[dict]:
        return [
            {
                "unit_id": item["unit_id"],
                "duration": item["duration"],
                "text": item["text"],
                "characters": item.get("characters", []),
                "is_entry": item.get("is_entry", False),
                "scene_context": item.get("scene_context", ""),
            }
            for item in items
        ]

    @classmethod
    def _review_context(
        cls, script_data: dict, script_meta: dict
    ) -> Tuple[List[dict], Optional[int]]:
        """返回可分镜消费的质检问题与建议总时长。

        - 正文修改后（stale）旧行号与旧时长都不可信，一律不注入；
        - 长文质检截断时问题清单仍可用，但总时长只代表节选，不得折算成每集目标。
        """
        review = script_data.get("import_review") if isinstance(script_data, dict) else None
        if not isinstance(review, dict) or review.get("stale"):
            return [], None
        issues = review.get("issues") if isinstance(review.get("issues"), list) else []
        coverage = review.get("coverage") if isinstance(review.get("coverage"), dict) else {}
        if coverage.get("truncated"):
            return issues, None
        return issues, (script_meta or {}).get("suggested_total_seconds")

    @classmethod
    def _episode_suggested_seconds(cls, script_context: dict) -> Optional[int]:
        """把全篇建议总时长折算成本集参考值；无建议时返回 None。"""
        try:
            suggested = int(script_context.get("suggested_total_seconds") or 0)
        except (TypeError, ValueError):
            suggested = 0
        if suggested <= 0:
            return None
        try:
            episode_count = max(1, int(script_context.get("episode_count") or 1))
        except (TypeError, ValueError):
            episode_count = 1
        return max(5, int(round(suggested / episode_count)))

    @classmethod
    def _build_planning_hint(cls, script_context: dict) -> str:
        """把导入质检的建议时长与问题清单转成分镜规划提示。"""
        parts: List[str] = []
        episode_seconds = cls._episode_suggested_seconds(script_context)
        if episode_seconds:
            parts.append(
                f"本集建议总时长约 {episode_seconds} 秒（按内容完整与节奏舒适优先，可上下浮动）；"
                f"请在 5-15 秒片段约束内合理规划片段数量，不要为凑时长删减或压缩内容。"
            )
        issues = script_context.get("review_issues")
        line_start = script_context.get("line_start")
        line_end = script_context.get("line_end")
        coords_ready = isinstance(line_start, int) and isinstance(line_end, int)
        # 坐标缺失时 fail-closed：宁可不用问题清单，也不把别集/全局行号注入
        if isinstance(issues, list) and issues and coords_ready:
            hints: List[str] = []
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                line_no = issue.get("line")
                # 多集时只注入本集行范围内的问题，避免把别集问题带进来
                if isinstance(line_no, int) and not (line_start <= line_no <= line_end):
                    continue
                problem = str(issue.get("problem") or "").strip()
                if not problem:
                    continue
                suggestion = str(issue.get("suggestion") or "").strip()
                # 单集正文没有行号标注，hint 不携带全局行号，避免跨集误映射
                suffix = f"（建议：{suggestion}）" if suggestion else ""
                hints.append(f"- {problem}{suffix}")
                if len(hints) >= 6:
                    break
            if hints:
                parts.append("剧本质检要点（分段与镜头设计时请规避）：\n" + "\n".join(hints))
        return "\n".join(parts) if parts else "无额外约束。"

    @classmethod
    def _build_segmentation_prompt(
        cls,
        ep_n: int,
        ep_t: str,
        annotated_script: str,
        characters: List[dict],
        settings: List[dict],
        retry_error: str = "",
        planning_hint: str = "",
    ) -> str:
        template = _get_storyboard_prompt("segment_plan", "zh")
        retry_feedback = ""
        if retry_error:
            retry_feedback = f"上一次输出未通过校验，错误原因：{retry_error}\n请根据这个错误修正输出。"
        from prompts.loader import format_prompt
        return format_prompt(
            template,
            episode_number=ep_n,
            episode_title=ep_t,
            annotated_script=annotated_script,
            asset_characters=json.dumps(characters, ensure_ascii=False),
            asset_settings=json.dumps(settings, ensure_ascii=False),
            retry_feedback=retry_feedback,
            planning_hint=planning_hint or "无额外约束。",
        )

    @classmethod
    def _build_segment_design_prompt(
        cls,
        ep_n: int,
        ep_t: str,
        plan: dict,
        style: str,
        retry_error: str = "",
        planning_hint: str = "",
    ) -> str:
        items_payload = cls._scene_item_payload(plan.get("items", []))
        segment_total = sum(int(item.get("duration") or 0) for item in plan.get("items", []))
        output_total = max(cls.MIN_SEGMENT_DURATION, segment_total)
        template = _get_storyboard_prompt("segment_design", "zh")
        retry_feedback = ""
        if retry_error:
            retry_feedback = f"上一次输出未通过校验，错误原因：{retry_error}\n请根据这个错误修正输出。"
        from prompts.loader import format_prompt
        return format_prompt(
            template,
            episode_number=ep_n,
            episode_title=ep_t,
            segment_number=plan.get("segment_number"),
            location=plan.get("location", ""),
            scene_time=plan.get("scene_time", ""),
            scene_space=plan.get("scene_space", ""),
            scene_context=plan.get("scene_context", ""),
            characters=json.dumps(plan.get("characters", []), ensure_ascii=False),
            total_duration=output_total,
            items=json.dumps(items_payload, ensure_ascii=False),
            style=style,
            retry_feedback=retry_feedback,
            planning_hint=planning_hint or "无额外约束。",
        )

    async def _query_json_array_with_retries(
        self,
        prompt: str,
        llm_model: str,
        sid: str,
        *,
        label: str,
        max_retries: int = 3,
    ) -> List[dict]:
        from models.llm_client import LLM

        loop = asyncio.get_running_loop()
        last_error: Optional[Exception] = None
        raw = ""
        for attempt in range(max_retries):
            try:
                llm = LLM()
                raw = await asyncio.to_thread(
                    self._cancellable_query,
                    llm,
                    prompt,
                    [],
                    llm_model,
                    False,
                    sid,
                    False,
                )
                extracted = self._extract_json_array(raw)
                if extracted is not None:
                    return extracted
                raise ValueError("模型输出不是 JSON 数组")
            except Exception as exc:
                last_error = exc
                logger.warning("[Storyboard] %s attempt %d failed: %s", label, attempt + 1, exc)
        logger.error("[Storyboard] %s failed after retries. Last raw: %s", label, raw[:2000])
        raise last_error or ValueError(f"{label} 失败")

    async def _query_json_object_with_retries(
        self,
        prompt: str,
        llm_model: str,
        sid: str,
        *,
        label: str,
        max_retries: int = 3,
    ) -> dict:
        from models.llm_client import LLM

        loop = asyncio.get_running_loop()
        last_error: Optional[Exception] = None
        raw = ""
        for attempt in range(max_retries):
            try:
                llm = LLM()
                raw = await asyncio.to_thread(
                    self._cancellable_query,
                    llm,
                    prompt,
                    [],
                    llm_model,
                    False,
                    sid,
                    False,
                )
                extracted = self._extract_json_object(raw)
                if extracted is not None:
                    return extracted
                raise ValueError("模型输出不是 JSON 对象")
            except Exception as exc:
                last_error = exc
                logger.warning("[Storyboard] %s attempt %d failed: %s", label, attempt + 1, exc)
        logger.error("[Storyboard] %s failed after retries. Last raw: %s", label, raw[:2000])
        raise last_error or ValueError(f"{label} 失败")

    @classmethod
    def _normalize_shot_type(cls, value: Any, *, first: bool = False) -> str:
        text = str(value or "")
        if "全景" in text:
            shot_type = "全景"
        elif "中景" in text:
            shot_type = "中景"
        elif "近景" in text or "特写" in text:
            shot_type = "近景"
        else:
            shot_type = "中景"
        if first and shot_type not in cls.OPENING_SHOT_TYPES:
            return "中景"
        return shot_type

    @classmethod
    def _shot_durations_for_plan(cls, plan: dict) -> List[int]:
        durations = [int(item.get("duration") or cls.MIN_SHOT_DURATION) for item in plan.get("items", [])]
        total = sum(durations)
        if durations and total < cls.MIN_SEGMENT_DURATION:
            durations[-1] += cls.MIN_SEGMENT_DURATION - total
        return durations

    @classmethod
    def _opening_camera_prefix(cls, shot_type: str, characters: List[str]) -> str:
        subject = "、".join(characters) if characters else "片段中的所有人物"
        return f"{shot_type}，平视机位，镜头同时拍到{subject}，站位：主要人物面向镜头或彼此成自然对话关系分布。"

    @classmethod
    def _normalize_segment_design(cls, ep_n: int, plan: dict, raw_design: dict) -> dict:
        items = plan.get("items", [])
        raw_shots = raw_design.get("shots") if isinstance(raw_design, dict) else None
        if not isinstance(raw_shots, list) or not raw_shots:
            raise ValueError("片段设计缺少 shots")

        by_id = {
            shot.get("unit_id"): shot
            for shot in raw_shots
            if isinstance(shot, dict) and isinstance(shot.get("unit_id"), str)
        }
        if by_id:
            missing = [item["unit_id"] for item in items if item["unit_id"] not in by_id]
            extra = [unit_id for unit_id in by_id if unit_id not in {item["unit_id"] for item in items}]
            if missing or extra:
                raise ValueError(f"片段设计 unit_id 不匹配，missing={missing}, extra={extra}")
            ordered_raw = [by_id[item["unit_id"]] for item in items]
        else:
            if len(raw_shots) != len(items):
                raise ValueError("片段设计 shots 数量与 items 不一致")
            ordered_raw = raw_shots

        durations = cls._shot_durations_for_plan(plan)
        shots: List[dict] = []
        characters = plan.get("characters", [])
        for idx, (item, raw_shot) in enumerate(zip(items, ordered_raw)):
            shot_type = cls._normalize_shot_type(raw_shot.get("shot_type"), first=(idx == 0))
            content = str(raw_shot.get("content") or "").strip()
            shots.append({
                "shot_number": idx + 1,
                "shot_type": shot_type,
                "duration": durations[idx],
                "content": content,
            })

        return cls._segment_from_shots(
            ep_n,
            int(plan.get("segment_number") or 1),
            plan.get("location", ""),
            shots,
            characters,
            plan.get("scene_time", ""),
            plan.get("scene_space", ""),
            plan.get("scene_context", ""),
        )

    @classmethod
    def _fallback_design_segment(cls, ep_n: int, plan: dict) -> dict:
        characters = plan.get("characters", [])
        durations = cls._shot_durations_for_plan(plan)
        shots: List[dict] = []
        for idx, item in enumerate(plan.get("items", [])):
            is_dialogue = bool(item.get("is_dialogue"))
            speaker = item.get("speaker") or "角色"
            shot_type = "中景" if idx == 0 or is_dialogue else "全景"
            if idx == 0:
                prefix = cls._opening_camera_prefix(shot_type, characters)
                content = f"{prefix}人物朝向彼此或镜头侧前方。{item['text']}"
            elif is_dialogue:
                content = f"{shot_type}，平视略侧机位拍摄{speaker}，人物面向对话对象。{item['text']}"
            else:
                subject = "、".join(item.get("characters") or characters) or "场景主体"
                content = f"{shot_type}，平视跟拍{subject}，人物沿动作方向移动。{item['text']}"
            shots.append({
                "shot_number": idx + 1,
                "shot_type": shot_type,
                "duration": durations[idx],
                "content": content,
            })
        return cls._segment_from_shots(
            ep_n,
            int(plan.get("segment_number") or 1),
            plan.get("location", ""),
            shots,
            characters,
            plan.get("scene_time", ""),
            plan.get("scene_space", ""),
            plan.get("scene_context", ""),
        )

    @staticmethod
    def _staging_continuity_payload(segments: List[dict]) -> List[dict]:
        payload: List[dict] = []
        for seg in segments:
            payload.append({
                "segment_number": seg.get("segment_number"),
                "location": seg.get("location", ""),
                "scene_context": seg.get("scene_context", ""),
                "scene_space": seg.get("scene_space", ""),
                "characters": seg.get("characters", []),
                "shots": [
                    {
                        "shot_number": shot.get("shot_number"),
                        "shot_type": shot.get("shot_type", ""),
                        "content": shot.get("content", ""),
                    }
                    for shot in seg.get("shots", [])
                    if isinstance(shot, dict)
                ],
            })
        return payload

    @classmethod
    def _build_staging_continuity_prompt(
        cls,
        ep_n: int,
        ep_t: str,
        segments: List[dict],
        retry_error: str = "",
    ) -> str:
        template = _get_storyboard_prompt("staging_continuity", "zh")
        retry_feedback = ""
        if retry_error:
            retry_feedback = f"上一次输出未通过校验，错误原因：{retry_error}\n请根据这个错误修正输出。"
        from prompts.loader import format_prompt
        return format_prompt(
            template,
            episode_number=ep_n,
            episode_title=ep_t,
            segments=json.dumps(cls._staging_continuity_payload(segments), ensure_ascii=False, indent=2),
            retry_feedback=retry_feedback,
        )

    @staticmethod
    def _apply_staging_continuity_patches(segments: List[dict], review: dict) -> int:
        patches = review.get("patches") if isinstance(review, dict) else None
        if not isinstance(patches, list):
            raise ValueError("站位连续性检查输出缺少 patches 数组")

        segment_by_number = {
            int(seg.get("segment_number") or 0): seg
            for seg in segments
            if isinstance(seg, dict)
        }
        updates: List[Tuple[dict, str]] = []
        for patch in patches:
            if not isinstance(patch, dict):
                raise ValueError("站位连续性补丁包含非对象元素")
            try:
                segment_number = int(patch.get("segment_number"))
                shot_number = int(patch.get("shot_number"))
            except (TypeError, ValueError):
                raise ValueError(f"站位连续性补丁编号无效: {patch}")
            content = str(patch.get("content") or "").strip()
            if not content:
                raise ValueError(f"站位连续性补丁 content 为空: {patch}")

            seg = segment_by_number.get(segment_number)
            if not seg:
                raise ValueError(f"站位连续性补丁引用未知片段: {segment_number}")
            shot = next(
                (item for item in seg.get("shots", []) if int(item.get("shot_number") or 0) == shot_number),
                None,
            )
            if not shot:
                raise ValueError(f"站位连续性补丁引用未知分镜: segment={segment_number}, shot={shot_number}")
            updates.append((shot, content))

        for shot, content in updates:
            shot["content"] = content
        return len(updates)

    async def _fix_episode_staging_continuity(
        self,
        ep_n: int,
        ep_t: str,
        segments: List[dict],
        llm_model: str,
        sid: str,
        *,
        progress: Optional["_StoryboardProgress"] = None,
        heartbeat: Optional[Callable[[Awaitable, str], Awaitable[Any]]] = None,
    ) -> List[dict]:
        last_error: Optional[Exception] = None
        for attempt in range(2):
            if attempt and progress is not None:
                # 重试是多出来的工作量：先登记权重，再发一条可观察的消息（不改重试/超时预算）
                progress.add_total(1)
                progress.note(f"第 {ep_n} 集人物站位连续性检查重试（第 {attempt + 1}/2 次）")
            try:
                prompt = self._build_staging_continuity_prompt(
                    ep_n,
                    ep_t,
                    segments,
                    retry_error=str(last_error) if last_error else "",
                )
                query = self._query_json_object_with_retries(
                    prompt,
                    llm_model,
                    sid,
                    label=f"第 {ep_n} 集人物站位连续性检查",
                    max_retries=1,
                )
                review = await (heartbeat(query, f"第 {ep_n} 集正在检查人物站位连续性，请稍候...")
                                if heartbeat else query)
                applied = self._apply_staging_continuity_patches(segments, review)
                issues = review.get("issues") if isinstance(review, dict) else []
                if applied:
                    logger.info("[Storyboard] Episode %s staging continuity fixed %d shots. issues=%s", ep_n, applied, issues)
                else:
                    logger.info("[Storyboard] Episode %s staging continuity passed.", ep_n)
                return segments
            except Exception as exc:
                last_error = exc
                logger.warning("[Storyboard] Episode %s staging continuity attempt %d failed: %s", ep_n, attempt + 1, exc)
            finally:
                # 一次尝试就是一步工作量：无论成功、模型失败还是校验失败都要记账，
                # 否则收尾的档位会被未记账的失败尝试留在 95 以下。
                if progress is not None:
                    progress.complete(1)
        logger.warning("[Storyboard] Episode %s staging continuity check skipped after retries: %s", ep_n, last_error)
        return segments

    async def _plan_episode_segments(
        self,
        ep_n: int,
        ep_t: str,
        annotated_script: str,
        units: List[dict],
        characters: List[dict],
        settings: List[dict],
        llm_model: str,
        sid: str,
        planning_hint: str = "",
    ) -> List[dict]:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                prompt = self._build_segmentation_prompt(
                    ep_n,
                    ep_t,
                    annotated_script,
                    characters,
                    settings,
                    retry_error=str(last_error) if last_error else "",
                    planning_hint=planning_hint,
                )
                raw_plan = await self._query_json_array_with_retries(
                    prompt,
                    llm_model,
                    sid,
                    label=f"第 {ep_n} 集片段规划",
                    max_retries=1,
                )
                return self._validate_segment_plan(ep_n, raw_plan, units)
            except Exception as exc:
                last_error = exc
                logger.warning("[Storyboard] Episode %s segment plan validation attempt %d failed: %s", ep_n, attempt + 1, exc)

        logger.warning("[Storyboard] Episode %s falling back to deterministic segment plan: %s", ep_n, last_error)
        return self._fallback_segment_plan(ep_n, units)

    async def _design_one_segment(
        self,
        ep_n: int,
        ep_t: str,
        plan: dict,
        style: str,
        llm_model: str,
        sid: str,
        planning_hint: str = "",
    ) -> dict:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                prompt = self._build_segment_design_prompt(
                    ep_n,
                    ep_t,
                    plan,
                    style,
                    retry_error=str(last_error) if last_error else "",
                    planning_hint=planning_hint,
                )
                raw_design = await self._query_json_object_with_retries(
                    prompt,
                    llm_model,
                    sid,
                    label=f"第 {ep_n} 集片段 {plan.get('segment_number')} 分镜设计",
                    max_retries=1,
                )
                return self._normalize_segment_design(ep_n, plan, raw_design)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "[Storyboard] Episode %s segment %s design attempt %d failed: %s",
                    ep_n,
                    plan.get("segment_number"),
                    attempt + 1,
                    exc,
                )

        logger.warning(
            "[Storyboard] Episode %s segment %s falling back to deterministic design: %s",
            ep_n,
            plan.get("segment_number"),
            last_error,
        )
        return self._fallback_design_segment(ep_n, plan)

    @staticmethod
    def _apply_duration_budget(
        units: List[dict],
        target_seconds: Any,
        ep_n: int,
        progress_note: Optional[Callable[[str], None]] = None,
        advisory: bool = False,
    ) -> List[dict]:
        """按目标时长缩放单元时长；advisory 时只提示，不牺牲内容与节奏。"""
        try:
            target = int(target_seconds or 0)
        except (TypeError, ValueError):
            target = 0
        if target <= 0 or not units:
            return units
        total = sum(int(unit.get("duration") or 0) for unit in units)
        if total <= target * 1.2:
            return units
        if advisory:
            if progress_note:
                progress_note(
                    f"第 {ep_n} 集按内容需要约 {total}s，超出原剧本时长 {target}s，按叙事完整优先保留"
                )
            return units
        ratio = target / total if total else 1
        for unit in units:
            unit["duration"] = max(2, int(round(int(unit.get("duration") or 2) * ratio)))
        scaled = sum(int(unit.get("duration") or 0) for unit in units)
        if scaled > target * 1.2 and progress_note:
            progress_note(
                f"第 {ep_n} 集内容超出目标时长（{scaled}s > {target}s），已按单元下限压缩"
            )
        return units

    async def _design_episode_storyboard(
        self,
        ep_n: int,
        ep_t: str,
        ep_c: str,
        characters: List[dict],
        settings: List[dict],
        style: str,
        llm_model: str,
        sid: str,
        progress_note: Optional[Callable[[str], None]] = None,
        script_context: Optional[dict] = None,
        *,
        progress: Optional["_StoryboardProgress"] = None,
    ) -> List[dict]:
        context = script_context or {}

        async def _heartbeat(step: Awaitable, message: str):
            """长耗时步骤期间的心跳推进；量程由单一进度源给出，推进值回到进度源裁决。"""
            if progress is None:
                return await step
            start, cap = progress.heartbeat_window()
            return await self._await_with_heartbeat(
                step,
                start=start,
                cap=cap,
                message=message,
                log=lambda percent, beat_message: progress.beat(beat_message, percent),
                interval=HEARTBEAT_INTERVAL_SECONDS,
            )

        annotated_script, units = self._annotate_episode_script(
            ep_c,
            characters,
            settings,
            annotations=context.get("annotations"),
            scene_spans=context.get("scene_spans"),
            line_offset=context.get("line_start"),
            baseline_line_count=context.get("baseline_line_count"),
        )
        if not units:
            raise Exception(f"第 {ep_n} 集未能识别出动作或台词")
        self._apply_duration_budget(
            units,
            context.get("target_duration_seconds"),
            ep_n,
            progress_note,
            advisory=bool(context.get("duration_budget_advisory")),
        )

        logger.info("[Storyboard] Episode %s annotated %d script units", ep_n, len(units))
        if progress_note:
            progress_note(f"第 {ep_n} 集已完成时长标注，正在划分片段")
        planning_hint = self._build_planning_hint(context)
        plans = await _heartbeat(
            self._plan_episode_segments(
                ep_n,
                ep_t,
                annotated_script,
                units,
                characters,
                settings,
                llm_model,
                sid,
                planning_hint=planning_hint,
            ),
            f"第 {ep_n} 集正在划分片段，请稍候...",
        )
        if not plans:
            raise Exception(f"第 {ep_n} 集片段规划失败")
        if progress is not None:
            # 真实片段数此时才已知：先补齐与估算值的差量（只增不减），再为本步记账
            progress.add_total(len(plans) - int(context.get("estimated_segments") or 0))
            progress.complete(1, f"第 {ep_n} 集已规划 {len(plans)} 个片段，正在设计分镜")
        elif progress_note:
            progress_note(f"第 {ep_n} 集已规划 {len(plans)} 个片段，正在设计分镜")

        logger.info("[Storyboard] Episode %s planned %d segments; designing in parallel", ep_n, len(plans))

        async def _tagged(index: int, awaitable) -> Tuple[int, dict]:
            return index, await awaitable

        tasks = [
            _tagged(index, self._design_one_segment(
                ep_n, ep_t, plan, style, llm_model, sid, planning_hint=planning_hint
            ))
            for index, plan in enumerate(plans)
        ]
        # 片段设计并发跑、但完成时刻各不相同：按完成逐个推进进度（gather 只能等全部返完）。
        # 每一项都经 _heartbeat 等待，长时间没有片段返回时进度也会渐近推进。
        # 输出顺序仍按 segment_number（同值时按原输入序）→ 与 gather 时代逐字一致。
        designed: List[Tuple[int, int, dict]] = []
        for finished in asyncio.as_completed(tasks):
            plan_index, segment = await _heartbeat(
                finished,
                f"第 {ep_n} 集分镜设计中（已返回 {len(designed)}/{len(plans)}），请稍候...",
            )
            designed.append((int(segment.get("segment_number") or 0), plan_index, segment))
            if progress is not None:
                progress.complete(
                    1,
                    f"第 {ep_n} 集分镜已设计完 {len(designed)}/{len(plans)} 个片段",
                )
            elif progress_note:
                progress_note(f"第 {ep_n} 集分镜已设计完 {len(designed)}/{len(plans)} 个片段")
        designed.sort(key=lambda item: (item[0], item[1]))
        segments = [item[2] for item in designed]
        logger.info("[Storyboard] Episode %s checking staging continuity", ep_n)
        if progress_note:
            progress_note(f"第 {ep_n} 集正在检查人物站位连续性")
        segments = await self._fix_episode_staging_continuity(
            ep_n, ep_t, segments, llm_model, sid, progress=progress, heartbeat=_heartbeat
        )
        return segments

    @classmethod
    def _estimate_duration(cls, text: str, is_dialogue: bool) -> int:
        """Legacy heuristic duration in seconds."""
        visible_text = re.sub(r"[“”\"'，。！？、,.!?；;：:\s]", "", text)
        if is_dialogue:
            duration = 3 + len(visible_text) // 18
        else:
            duration = 3 + len(visible_text) // 34
        return max(cls.MIN_SHOT_DURATION, min(duration, cls.MAX_SEGMENT_DURATION))

    @staticmethod
    def _infer_shot_type(text: str, is_dialogue: bool) -> str:
        if re.search(r"城市|办公室|窗外|全景|拉远|场景|夜景|两台电脑|屏幕并排", text):
            return "全景"
        if re.search(r"特写|屏幕|手机|键盘|终端|报表|手指|代码|PASS|眼睛|嘴角|表情", text):
            return "近景"
        if is_dialogue:
            return "中景"
        return "中景"

    @classmethod
    def _normalize_first_shot_type(cls, shots: List[dict]) -> None:
        if not shots:
            return
        if shots[0].get("shot_type") not in cls.OPENING_SHOT_TYPES:
            shots[0]["shot_type"] = "中景"
            content = shots[0].get("content", "")
            shots[0]["content"] = re.sub(r"^(近景|过肩近景|特写|大特写)", "中景", content, count=1)

    @classmethod
    def _ensure_segment_duration(cls, shots: List[dict]) -> int:
        total = sum(int(shot.get("duration") or cls.MIN_SHOT_DURATION) for shot in shots)
        if shots and total < cls.MIN_SEGMENT_DURATION:
            shots[-1]["duration"] += cls.MIN_SEGMENT_DURATION - total
            total = cls.MIN_SEGMENT_DURATION
        return min(total, cls.MAX_SEGMENT_DURATION)

    @classmethod
    def _make_shot_content(
        cls,
        *,
        shot_type: str,
        line: str,
        characters: List[str],
        is_dialogue: bool,
        speaker: str = "",
        tone: str = "",
    ) -> str:
        if is_dialogue:
            tone_text = tone or "自然、贴合当下情绪"
            return f"{shot_type}，镜头对准{speaker or '角色'}，呈现动作与表情变化。{speaker}说：“{line}”。音色：{tone_text}。"
        subject = "、".join(characters) if characters else "场景主体"
        return f"{shot_type}，镜头呈现{subject}。{line}"

    @classmethod
    def _segment_from_shots(
        cls,
        ep_n: int,
        segment_number: int,
        location: str,
        shots: List[dict],
        characters: List[str],
        scene_time: str = "",
        scene_space: str = "",
        scene_context: str = "",
    ) -> dict:
        for idx, shot in enumerate(shots, 1):
            shot["shot_number"] = idx
            shot["duration"] = min(
                cls.MAX_SEGMENT_DURATION,
                max(cls.MIN_SHOT_DURATION, int(shot.get("duration") or cls.MIN_SHOT_DURATION)),
            )
        cls._normalize_first_shot_type(shots)
        total_duration = cls._ensure_segment_duration(shots)
        segment = {
            "segment_id": f"seg_{ep_n:02d}_{segment_number:02d}",
            "segment_number": segment_number,
            "total_duration": total_duration,
            "location": location,
            "characters": characters,
            "shots": shots,
            "episode_number": ep_n,
        }
        if scene_time:
            segment["scene_time"] = scene_time
        if scene_space:
            segment["scene_space"] = scene_space
        if scene_context:
            segment["scene_context"] = scene_context
        return segment

    @classmethod
    def _estimate_segment_count(
        cls, ep_n: int, script_text: str, characters: List[dict], settings: List[dict]
    ) -> int:
        """进度权重用的片段数估算：走已有的纯文本拆分路径，不调模型。

        该路径本身与模型输出同构（Segment -> Shots），拿它的条数当分母初值可以把
        “真实片段数”的误差压小；估算失败只影响进度精度，不得影响执行，因此异常一律兜底为 1。
        """
        try:
            count = len(cls._build_segments_by_regex(ep_n, script_text, characters, settings))
        except Exception as exc:  # noqa: BLE001 - 估算不参与产物构造，不得抹掉本轮执行
            logger.warning("[Storyboard] Episode %s segment estimate failed: %s", ep_n, exc)
            count = 0
        return max(1, count)

    @classmethod
    def _build_segments_by_regex(
        cls,
        ep_n: int,
        script_text: str,
        characters: List[dict],
        settings: List[dict],
    ) -> List[dict]:
        """Deterministically parse script text into model-ready segments.

        This path intentionally mirrors the LLM prompt output:
        segment_number, total_duration, location, characters, shots[].
        If it cannot extract useful shots, the caller falls back to LLM.
        """
        character_names = cls._character_names(characters)
        setting_names = cls._setting_names(settings)
        alias_map = cls._alias_map(settings)
        raw_lines = [cls._strip_markup(line) for line in script_text.replace("\r\n", "\n").split("\n")]

        atomic_shots: List[dict] = []
        current_location = cls._canonical_location(
            setting_names[0] if setting_names else "", alias_map
        )
        current_scene_time = ""
        current_scene_space = ""
        current_scene_context = ""
        scene_characters: List[str] = []
        scene_key = 0
        found_scene_header = False

        for raw_line in raw_lines:
            line = cls._clean_script_line(raw_line)
            if not line or cls._is_metadata_line(line) or cls._is_end_marker(line):
                continue
            if re.match(r"^第?\d+集$", line):
                continue

            header_parts = cls._parse_scene_header_parts(line, setting_names)
            if header_parts:
                found_scene_header = True
                scene_key += 1
                current_location = cls._canonical_location(
                    header_parts.get("location", current_location), alias_map
                )
                current_scene_time = header_parts.get("scene_time", "")
                current_scene_space = header_parts.get("scene_space", "")
                current_scene_context = header_parts.get("scene_context", line)
                matched = cls._match_characters(line, character_names)
                if matched:
                    scene_characters = matched
                continue

            if re.match(r"^(?:人物|Characters)\s*[:：]", line, flags=re.I):
                scene_characters = cls._match_characters(line, character_names)
                continue

            matched_chars = cls._match_characters(line, character_names)
            shot_chars = matched_chars or scene_characters
            dialogue = cls._dialogue_parts(line)
            is_dialogue = dialogue is not None
            speaker = ""
            tone = ""
            shot_text = line
            if dialogue:
                speaker, tone, shot_text = dialogue
                speaker_matches = cls._match_characters(speaker, character_names)
                if speaker_matches:
                    shot_chars = list(dict.fromkeys(speaker_matches + shot_chars))
                elif speaker in {"旁白", "独白", "画外音"}:
                    shot_chars = shot_chars or scene_characters

            shot_type = cls._infer_shot_type(line, is_dialogue)
            duration = cls._estimate_duration(shot_text, is_dialogue)
            location = cls._resolve_location(line, current_location, setting_names)

            atomic_shots.append({
                "scene_key": scene_key,
                "location": location,
                "scene_time": current_scene_time,
                "scene_space": current_scene_space,
                "scene_context": current_scene_context,
                "characters": shot_chars,
                "shot": {
                    "shot_number": 0,
                    "shot_type": shot_type,
                    "duration": duration,
                    "content": cls._make_shot_content(
                        shot_type=shot_type,
                        line=shot_text,
                        characters=shot_chars,
                        is_dialogue=is_dialogue,
                        speaker=speaker,
                        tone=tone,
                    ),
                },
            })

        if not found_scene_header or not atomic_shots:
            return []

        segments: List[dict] = []
        current_items: List[dict] = []
        current_scene_key: Optional[int] = None
        current_location = ""
        current_scene_time = ""
        current_scene_space = ""
        current_scene_context = ""
        current_chars: List[str] = []

        def flush_current():
            nonlocal current_items, current_scene_key, current_location
            nonlocal current_scene_time, current_scene_space, current_scene_context, current_chars
            if not current_items:
                return
            shots = [item["shot"] for item in current_items]
            chars: List[str] = []
            for item in current_items:
                for name in item["characters"]:
                    if name not in chars:
                        chars.append(name)
            if not chars:
                chars = current_chars
            segments.append(cls._segment_from_shots(
                ep_n,
                len(segments) + 1,
                current_location,
                shots,
                chars,
                current_scene_time,
                current_scene_space,
                current_scene_context,
            ))
            current_items = []
            current_scene_key = None
            current_location = ""
            current_scene_time = ""
            current_scene_space = ""
            current_scene_context = ""
            current_chars = []

        for item in atomic_shots:
            item_duration = int(item["shot"]["duration"])
            current_duration = sum(int(existing["shot"]["duration"]) for existing in current_items)
            scene_changed = current_items and (
                item["scene_key"] != current_scene_key or item["location"] != current_location
            )
            would_overflow = current_items and current_duration + item_duration > cls.MAX_SEGMENT_DURATION

            # A scene switch always starts a new video-model call. Otherwise greedily
            # pack shots until the next one would exceed the 15s upper bound.
            if scene_changed or would_overflow:
                flush_current()

            current_items.append(item)
            current_scene_key = item["scene_key"]
            current_location = item["location"]
            current_scene_time = item.get("scene_time", "")
            current_scene_space = item.get("scene_space", "")
            current_scene_context = item.get("scene_context", "")
            for name in item["characters"]:
                if name not in current_chars:
                    current_chars.append(name)

        flush_current()
        return segments

    @classmethod
    def _normalize_llm_segments(cls, ep_n: int, extracted: List[dict]) -> List[dict]:
        valid_segments = []
        for seg in extracted:
            if not isinstance(seg, dict):
                continue

            shots = seg.get("shots", [])
            pending_shots = []
            for s in shots:
                if not isinstance(s, dict):
                    continue
                dur = s.get("duration", cls.MIN_SHOT_DURATION)
                try:
                    dur = int(dur)
                except (TypeError, ValueError):
                    dur = cls.MIN_SHOT_DURATION
                pending_shots.append({
                    "shot_number": 0,
                    "shot_type": s.get("shot_type", "中景"),
                    "duration": max(cls.MIN_SHOT_DURATION, dur),
                    "content": s.get("content", "")
                })

            if not pending_shots:
                continue

            chunk: List[dict] = []

            def flush_chunk():
                nonlocal chunk
                if not chunk:
                    return
                valid_segments.append(cls._segment_from_shots(
                    ep_n,
                    len(valid_segments) + 1,
                    seg.get("location", ""),
                    chunk,
                    seg.get("characters", []),
                ))
                chunk = []

            for shot in pending_shots:
                chunk_duration = sum(int(item.get("duration") or cls.MIN_SHOT_DURATION) for item in chunk)
                if chunk and chunk_duration + int(shot["duration"]) > cls.MAX_SEGMENT_DURATION:
                    flush_chunk()
                chunk.append(shot)
            flush_chunk()
        return valid_segments

    @staticmethod
    def _validate_episodes(episodes: List[dict]) -> List[dict]:
        """验证嵌套的 Episode -> Segment -> Shots 结构"""
        valid_episodes = []
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            
            segments = ep.get("segments", [])
            valid_segments = []
            for idx, seg in enumerate(segments, 1):
                if not isinstance(seg, dict):
                    continue
                
                shots = seg.get("shots", [])
                valid_shots = []
                calc_total_duration = 0
                
                for s in shots:
                    if not isinstance(s, dict):
                        continue
                    dur = s.get("duration", 5)
                    calc_total_duration += dur
                    valid_shots.append({
                        "shot_number": s.get("shot_number", len(valid_shots) + 1),
                        "shot_type": s.get("shot_type", "中景"),
                        "duration": dur,
                        "content": s.get("content", "")
                    })
                
                valid_segments.append({
                    "segment_id": seg.get("segment_id", f"seg_{str(idx).zfill(8)}"),
                    "segment_number": seg.get("segment_number", len(valid_segments) + 1),
                    "total_duration": seg.get("total_duration", calc_total_duration),
                    "location": seg.get("location", ""),
                    "scene_time": seg.get("scene_time", ""),
                    "scene_space": seg.get("scene_space", ""),
                    "scene_context": seg.get("scene_context", ""),
                    "characters": seg.get("characters", []),
                    "shots": valid_shots
                })
            
            episode_payload = {
                "episode_number": ep.get("episode_number", len(valid_episodes) + 1),
                "episode_title": ep.get("episode_title", ""),
                "segments": valid_segments,
            }
            if ep.get("suggested_total_seconds") is not None:
                episode_payload["suggested_total_seconds"] = ep["suggested_total_seconds"]
            valid_episodes.append(episode_payload)
        return valid_episodes

    @staticmethod
    def _as_episode_number(value: Any) -> Optional[int]:
        """集号归一：非法值返回 None（重跑集合不参与匹配，不抛错）。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _rerun_episode_numbers(intervention: Optional[Dict]) -> Set[int]:
        """显式重跑集号（openspec: script-manual-edit-resync）。

        分镜正文手改后 `ready_eps` 会跳过已有 segments 的集，用户无法靠「继续生成」重做
        该集；因此剧本页提供显式「重跑第 N 集分镜」，经 `/intervene` 以 `regenerate_episodes`
        传入。非法集号静默忽略（不阻断本次执行）。
        """
        values = (intervention or {}).get("regenerate_episodes")
        if not isinstance(values, list):
            return set()
        numbers = {StoryboardAgent._as_episode_number(value) for value in values}
        return {number for number in numbers if number is not None}

    async def process(self, input_data: Any, intervention: Optional[Dict] = None) -> Dict:
        input_data = self._merge_session_params(input_data)
        sid = input_data.get("session_id")
        if not sid:
            raise Exception("Missing session_id")
        artifacts = self._session_artifacts(input_data)
        session_meta = self._session_meta(input_data)
            
        llm_model = input_data.get("llm_model") or session_meta.get("llm_model")
        if not llm_model:
            raise ValueError("Missing required model configuration: llm_model")
        style = input_data.get("style") or session_meta.get("style") or "anime"
        
        # 处理人工干预/修改
        if intervention and "modified_storyboard" in intervention:
            modified_episodes = intervention["modified_storyboard"]
            if isinstance(modified_episodes, str):
                modified_episodes = json.loads(modified_episodes)
            return {
                "payload": {
                    "session_id": sid,
                    "episodes": modified_episodes,
                    "user_modified": True,
                    "updated_at": datetime.now().isoformat(),
                },
                "stage_completed": True,
            }
        
        script_data = artifacts.get("script_generation", {})
        if not script_data:
            raise Exception("未找到剧本数据")
        
        episodes = script_data.get("episodes", [])
        if not episodes:
            raise Exception("剧本数据中不包含有效集数列表(episodes)")

        # 检查是否有已存在的分镜数据，识别需要生成的集数
        existing_storyboard = artifacts.get("storyboard", {})
        existing_story_eps = existing_storyboard.get("episodes", [])
        
        # 建立已生成的 segments 索引
        ready_eps = {e["episode_number"] for e in existing_story_eps if e.get("segments")}
        
        # 确定需要处理的集数：该集还没有 segments，或该集被显式要求重跑
        rerun_eps = self._rerun_episode_numbers(intervention)
        episodes_to_proc = [
            ep for ep in episodes
            if ep.get("episode_number") not in ready_eps
            or self._as_episode_number(ep.get("episode_number")) in rerun_eps
        ]
        if rerun_eps:
            logger.info("[Storyboard] 显式重跑集：%s", sorted(rerun_eps))
        
        if not episodes_to_proc:
            logger.info("[Storyboard] All episodes already have storyboard segments. Skipping generation.")
            return {"payload": {"session_id": sid, "episodes": existing_story_eps}, "stage_completed": True}

        chars = script_data.get("characters", [])
        sets = script_data.get("settings", [])

        total_to_process = len(episodes_to_proc)
        completed_count = 0
        # 单一进度源：运行期的 percent 只由 _StoryboardProgress 计算与发送，
        # 调用方不再自行换算档位（原先「只按已完成集数推进」就是进度钉在 10% 的根因）。
        progress = _StoryboardProgress(
            lambda message, percent, data=None: self._report_progress(
                STORYBOARD_PHASE, message, percent, data
            ),
            min_interval=PROGRESS_MIN_EVENT_INTERVAL,
        )
        # 初始权重：每集 1 次片段规划 + 估算片段数 + 1 次站位连续性。
        # 估算走已有的纯文本拆分路径（不调模型），把分母做准，真实片段数返回后只增不减地补齐。
        estimates = [
            self._estimate_segment_count(
                self._as_episode_number(ep.get("episode_number")) or 0,
                ep.get("content", ""),
                chars,
                sets,
            )
            for ep in episodes_to_proc
        ]
        progress.add_total(sum(2 + estimate for estimate in estimates))
        progress.begin(f"开始设计 {total_to_process} 集的分镜...")

        async def proc_ep(ep, estimated: int):
            ep_n = ep.get("episode_number", 1)
            ep_t = ep.get("act_title", f"第{ep_n}集")
            ep_c = ep.get("content", "")
            script_meta = script_data.get("meta") if isinstance(script_data.get("meta"), dict) else {}
            baseline_text = str(script_meta.get("original_text") or "")
            review_issues, suggested_total_seconds = self._review_context(script_data, script_meta)
            script_context = {
                "annotations": script_data.get("line_annotations"),
                "scene_spans": script_data.get("scene_spans"),
                "line_start": ep.get("line_start"),
                "line_end": ep.get("line_end"),
                "baseline_line_count": len(baseline_text.split("\n")) if baseline_text else None,
                "target_duration_seconds": script_meta.get("target_duration_seconds"),
                # 导入剧本的目标时长只作提示，不压缩已按台词规划的单元时长
                "duration_budget_advisory": script_meta.get("duration_budget") == "advisory",
                # 质检建议只软引导分段规划；stale/截断时上游已返回空值
                "suggested_total_seconds": suggested_total_seconds,
                "episode_count": len(episodes),
                "review_issues": review_issues,
                # 进度权重补登记用的估算值：规划返回真实片段数后由进度源算差量
                "estimated_segments": estimated,
            }
            progress.note(f"正在处理第 {ep_n} 集分镜")
            segments = await self._design_episode_storyboard(
                ep_n,
                ep_t,
                ep_c,
                chars,
                sets,
                style,
                llm_model,
                sid,
                progress_note=lambda message: progress.note(message),
                script_context=script_context,
                progress=progress,
            )

            return {
                "episode_number": ep_n,
                "episode_title": ep_t,
                "segments": segments,
                "suggested_total_seconds": self._episode_suggested_seconds(script_context),
            }

        # 核心：支持流式推送增量产物，让前端能看到实时进度
        updated_ep_map = {e["episode_number"]: e for e in existing_story_eps}
        
        # 报告一次进度，带上 assets_preview 让编排器更新内存中的初步数据
        progress.note("准备生成分镜...", data={
            "assets_preview": {
                "session_id": sid,
                "episodes": sorted(updated_ep_map.values(), key=lambda x: x["episode_number"]),
                "created_at": datetime.now().isoformat(),
            },
            "persist": True,
        })

        results_queue = [
            asyncio.create_task(proc_ep(ep, estimates[index]))
            for index, ep in enumerate(episodes_to_proc)
        ]

        for coro in asyncio.as_completed(results_queue):
            res = await coro
            completed_count += 1
            updated_ep_map[res["episode_number"]] = res

            # 每完成一集分镜，通过编排器更新内存并受控持久化
            temp_eps = sorted(updated_ep_map.values(), key=lambda x: x["episode_number"])

            # 集完成仍作为可持久化的里程碑（落库节奏与改动前逐字一致）；
            # 档位的推进已不依赖它，MUST NOT 在这里另算一套 percent。
            progress.note(
                f"已完成 {completed_count}/{total_to_process} 集分镜：第 {res['episode_number']} 集",
                data={
                    "assets_preview": {
                        "session_id": sid,
                        "episodes": temp_eps,
                        "created_at": datetime.now().isoformat(),
                    },
                    "persist": True,
                },
            )

        final_all_episodes = sorted(updated_ep_map.values(), key=lambda x: x["episode_number"])

        progress.finish()
        return {"payload": {"session_id": sid, "episodes": final_all_episodes}, "stage_completed": True}
