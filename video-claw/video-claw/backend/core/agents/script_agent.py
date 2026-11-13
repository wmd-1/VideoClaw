# -*- coding: utf-8 -*-
"""
阶段1: 编剧智能体 (直出一遍过版本)
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from prompts.loader import load_prompt_with_fallback

from .. import script_import, script_review, script_revision
from .base_agent import AgentInterface

logger = logging.getLogger(__name__)

def _get_script_prompt(name: str, lang: str = "zh") -> str:
    return load_prompt_with_fallback("script", name, lang, "zh")


# 全剧修订的上下文字符上限：超出时要求改用单集修订，不静默截断
SCRIPT_REVISION_MAX_CHARS = 60000

# 剧本 AI 质检的字符上限：单集不超过 8000，全剧不超过 10000；超出时拒绝，不静默截断。
# 当前尚未实现按集分批，因此错误文案明确提示“缩短/减少集数或等待分批支持”，不承诺不存在的功能。
SCRIPT_REVIEW_MAX_CHARS = 8000
SCRIPT_REVIEW_MAX_TOTAL_CHARS = 10000

# 质检输出是多条带原文/改写的 JSON，长输出容易触发自定义模型 300s 读超时；
# 仅本次质检调用放宽到 600s，不影响其它阶段的默认超时。
SCRIPT_REVIEW_TIMEOUT = 600.0
# 长剧本按实际 prompt 字符数动态放宽；上限 1800s（含提示词/角色/场景/行号）。
SCRIPT_REVIEW_TIMEOUT_MAX = 1800.0


def _review_aux_fingerprint(artifact: Dict[str, Any]) -> str:
    """只覆盖用户可编辑的摘要；派生标注/场景映射不从指纹阻塞撤销。"""
    payload = {"summary": artifact.get("summary")}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def script_review_timeout_for_chars(prompt_chars: Any) -> float:
    """按实际 prompt 字符数计算质检超时：600s 起步，约 6 字符/秒，封顶 1800s。"""
    try:
        chars = max(0, int(prompt_chars))
    except (TypeError, ValueError):
        chars = 0
    return max(SCRIPT_REVIEW_TIMEOUT, min(SCRIPT_REVIEW_TIMEOUT_MAX, chars / 6.0))


def review_episode_size(episodes: Any) -> tuple[int, int]:
    """校验单集/全剧字符上限，返回 (全剧总字符, 单集最大字符)。

    上限按 episodes[].content 统计，不含提示词/角色/场景/行号；超时另按实际
    prompt 长度计算，避免把固定提示词开销漏掉。
    """
    sizes = []
    for index, episode in enumerate(episodes or [], start=1):
        if not isinstance(episode, dict):
            continue
        number = episode.get("episode_number") or index
        sizes.append((number, len(str(episode.get("content", "")))))
    if not sizes:
        return 0, 0
    total = sum(size for _number, size in sizes)
    number, largest = max(sizes, key=lambda item: item[1])
    if largest > SCRIPT_REVIEW_MAX_CHARS:
        raise ValueError(
            f"第{number}集约 {largest} 字符，超出单集质检上限 {SCRIPT_REVIEW_MAX_CHARS}；"
            "请缩短该集，或等待分批质检支持"
        )
    if total > SCRIPT_REVIEW_MAX_TOTAL_CHARS:
        raise ValueError(
            f"全剧约 {total} 字符（单集最长 {largest}，未超 {SCRIPT_REVIEW_MAX_CHARS}），"
            f"超出单次质检总上限 {SCRIPT_REVIEW_MAX_TOTAL_CHARS}；"
            "请减少集数/缩短剧本，或等待分批质检支持"
        )
    return total, largest

# 片子类型规范枚举：meta.film_type 的唯一合法取值（openspec: script-stage-promo-type-rework）
FILM_TYPES = ("介绍", "宣传", "营销")
DEFAULT_FILM_TYPE = "介绍"

# 主体形式规范枚举：meta.subject_mode 的唯一合法取值，决定是否以人物为故事主体
SUBJECT_MODES = ("人物故事", "旁白描述")
DEFAULT_SUBJECT_MODE = "旁白描述"

# 判定输出同义词白名单：英文模板输出英文类型词，统一归一化回中文规范名
# （包含匹配按“具体优先”排序，避免理由句中的泛词误命中）
_FILM_TYPE_ALIASES = {
    "介绍": "介绍",
    "宣传": "宣传",
    "营销": "营销",
    "intro": "介绍",
    "introduction": "介绍",
    "introduce": "介绍",
    "introducing": "介绍",
    "promo": "宣传",
    "promotion": "宣传",
    "promotional": "宣传",
    "publicity": "宣传",
    "marketing": "营销",
    "sale": "营销",
    "sales": "营销",
    "selling": "营销",
}

_SUBJECT_MODE_ALIASES = {
    "旁白描述": "旁白描述",
    "旁白": "旁白描述",
    "voiceover description": "旁白描述",
    "voiceover": "旁白描述",
    "voice-over": "旁白描述",
    "voice over": "旁白描述",
    "narration": "旁白描述",
    "narrative": "旁白描述",
    "人物故事": "人物故事",
    "人物": "人物故事",
    "故事": "人物故事",
    "character story": "人物故事",
    "character_story": "人物故事",
    "person story": "人物故事",
    "story": "人物故事",
}


def _match_alias_by_position(text: str, aliases: dict[str, str]) -> Optional[str]:
    """确定性包含匹配：最早出现优先，同位置取更长别名。

    结果不依赖字典/集合的枚举顺序；同一位置同长度只可能是同一个别名。
    """
    best_pos: Optional[int] = None
    best_alias = ""
    best_canonical: Optional[str] = None
    for alias, canonical in aliases.items():
        pos = text.find(alias)
        if pos < 0:
            continue
        if (
            best_pos is None
            or pos < best_pos
            or (pos == best_pos and len(alias) > len(best_alias))
        ):
            best_pos, best_alias, best_canonical = pos, alias, canonical
    return best_canonical


class ScriptWriterAgent(AgentInterface):
    MIN_EPISODE_LINES = 25
    MAX_EPISODE_LINES = 30

    def __init__(self):
        super().__init__(name="ScriptWriter")

    @staticmethod
    def _extract_json_from_text(text: str) -> Optional[Any]:
        text = text.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试匹配第一个 { 或 [ 到底部对应的 } 或 ]
        start_obj = text.find('{')
        start_arr = text.find('[')
        
        # 确定起始位置
        if start_obj == -1 and start_arr == -1:
            return None
        
        start = start_obj if (start_obj != -1 and (start_arr == -1 or start_obj < start_arr)) else start_arr
        end_char = '}' if start == start_obj else ']'
        end = text.rfind(end_char)

        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _normalize_film_type(raw: Any) -> Optional[str]:
        """将判定输出归一化为中文规范枚举；无法识别返回 None（由调用方回退默认值）。"""
        if not isinstance(raw, str):
            return None
        text = raw.strip().strip("`\"'[](){}【】。.，,：:！!？?\n\r\t ").lower()
        if not text:
            return None
        if text in FILM_TYPES:
            return text
        if text in _FILM_TYPE_ALIASES:
            return _FILM_TYPE_ALIASES[text]
        # 包含匹配：LLM 可能在类型词前后附加理由文字；按“最早出现、同位置取更长”
        # 的确定性规则（不依赖字典枚举顺序）
        return _match_alias_by_position(text, _FILM_TYPE_ALIASES)

    @staticmethod
    def _normalize_subject_mode(raw: Any) -> Optional[str]:
        """将主体形式判定输出归一化为规范枚举；无法识别返回 None（由调用方回退默认值）。"""
        if not isinstance(raw, str):
            return None
        text = raw.strip().strip("`\"'[](){}【】。.，,：:！!？?\n\r\t ").lower()
        if not text:
            return None
        if text in SUBJECT_MODES:
            return text
        if text in _SUBJECT_MODE_ALIASES:
            return _SUBJECT_MODE_ALIASES[text]
        # 包含匹配：同样按“最早出现、同位置取更长”的确定性规则
        return _match_alias_by_position(text, _SUBJECT_MODE_ALIASES)

    @classmethod
    def _parse_classification(cls, raw_text: str) -> tuple:
        """解析类型判定输出（JSON 优先），返回 (film_type, subject_mode)，无法识别项为 None。"""
        film_type: Optional[str] = None
        subject_mode: Optional[str] = None
        parsed = cls._extract_json_from_text(raw_text)
        if isinstance(parsed, dict):
            for key in ("film_type", "type", "类型"):
                if key in parsed:
                    film_type = cls._normalize_film_type(parsed[key]) or film_type
            for key in ("subject_mode", "mode", "主体形式", "主体"):
                if key in parsed:
                    subject_mode = cls._normalize_subject_mode(parsed[key]) or subject_mode
            # 键名异常时扫描全部值兜底
            for value in parsed.values():
                film_type = film_type or cls._normalize_film_type(value)
                subject_mode = subject_mode or cls._normalize_subject_mode(value)
        if not film_type or not subject_mode:
            # 非 JSON 输出：按行扫描，两个维度各自取首个命中行
            for line in raw_text.splitlines():
                film_type = film_type or cls._normalize_film_type(line)
                subject_mode = subject_mode or cls._normalize_subject_mode(line)
        if not film_type or not subject_mode:
            film_type = film_type or cls._normalize_film_type(raw_text)
            subject_mode = subject_mode or cls._normalize_subject_mode(raw_text)
        return film_type, subject_mode

    def _gen_id(self, prefix: str = "char") -> str:
        import uuid
        return f"{prefix}_{uuid.uuid4().hex[:6]}"

    def _continuation_language(self, input_data, body: str = "", title: str = "") -> str:
        """续写语言与会话语言一致；标题优先取会话元数据。"""
        effective_title = title or str(self._session_meta(input_data).get("title") or "")
        return "zh" if self._session_language(input_data, body=body, title=effective_title) == "zh" else "en"

    @staticmethod
    def _evaluation_concurrency(input_data: dict) -> bool:
        """评估并行开关：复用会话 enable_concurrency，默认开启。"""
        return bool(input_data.get("enable_concurrency", True))

    async def _evaluate_pair(
        self, llm, dialogue_prompt: str, plot_prompt: str, llm_model: str,
        sid: str, web_search: bool, enable_concurrency: bool,
    ) -> tuple:
        """台词/情节评估并行；复用会话 enable_concurrency，固定返回顺序。"""
        async def _call(prompt: str) -> str:
            return await asyncio.to_thread(
                self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search,
            )

        if enable_concurrency:
            # 并行时两条都已提交；失败会等另一条结束再抛出，可能多一次调用
            results = await asyncio.gather(
                _call(dialogue_prompt), _call(plot_prompt), return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            dialogue, plot = results
        else:
            dialogue = await _call(dialogue_prompt)
            plot = await _call(plot_prompt)
        return dialogue, plot

    def _save_result(self, json_data: dict, sid: str, is_zh: bool):
        """调试用副本：编排器提供执行根时写入执行根，恢复只认 DB 视图。"""
        from config import settings as app_settings
        out_dir = self._output_dir(os.path.join(app_settings.RESULT_DIR, 'script'), 'script')
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f'{sid}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({k: v for k, v in json_data.items() if not str(k).startswith('_')}, f, ensure_ascii=False, indent=2)
        logger.info(f"[ScriptWriter] script saved to {out_path}")

    def _save_progress(self, sid: str, phase: str, data: dict):
        pass

    async def _run_revise_script(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """按指令修订剧本，只产出预览，不写产物。"""
        import copy

        floor = 0.0

        def _log(percent: float, message: str):
            # 只前进不后退：重试分支的档位若调整不当会倒挂，前端虽有取大兜底，
            # overlay/阶段快照仍会记录倒退值；这里再夹一层保证任何路径单调。
            nonlocal floor
            floor = max(floor, float(percent))
            self._report_progress("AI 修改", message, floor)

        _log(5, "正在读取剧本产物...")
        sid = input_data.get("session_id", "")
        instruction = str(intervention.get("instruction") or "").strip()
        scope = str(intervention.get("scope") or "all")
        if not instruction:
            raise ValueError("修改方向不能为空")
        if scope not in ("all", "episode"):
            raise ValueError("修改范围必须是 all 或 episode")

        current = self._session_artifact(input_data, "script_generation")
        if not current:
            raise ValueError("剧本产物不存在，无法修订")
        episodes = current.get("episodes") or []
        if not episodes:
            raise ValueError("剧本产物不包含剧集")

        target_number: Optional[int] = None
        target_episode: Optional[dict] = None
        if scope == "episode":
            try:
                target_number = int(intervention.get("episode_number"))
            except (TypeError, ValueError):
                raise ValueError("单集修订必须指定合法集号")
            for episode in episodes:
                number = episode.get("episode_number", episode.get("act_number"))
                try:
                    if int(number) == target_number:
                        target_episode = episode
                        break
                except (TypeError, ValueError):
                    continue
            if target_episode is None:
                raise ValueError(f"第 {target_number} 集不存在")
        else:
            total_chars = sum(len(str(ep.get("content", ""))) for ep in episodes)
            if total_chars > SCRIPT_REVISION_MAX_CHARS:
                raise ValueError(
                    f"剧本约 {total_chars} 字符，超出全剧修订上限 "
                    f"{SCRIPT_REVISION_MAX_CHARS}；请改用单集修订"
                )

        meta = current.get("meta") if isinstance(current.get("meta"), dict) else {}
        film_type = self._normalize_film_type(meta.get("film_type")) or DEFAULT_FILM_TYPE
        subject_mode = self._normalize_subject_mode(meta.get("subject_mode")) or DEFAULT_SUBJECT_MODE
        is_zh = self._session_language(input_data, instruction=instruction) == "zh"
        llm_model = self._require_input(input_data, "llm_model")
        web_search = input_data.get("web_search", False)

        from models.llm_client import LLM

        llm = LLM()
        loop = asyncio.get_running_loop()

        async def _call(prompt: str) -> str:
            # 修订会写回正文：safe_content=False 禁止 full_to_half 改写原文标点
            return await asyncio.to_thread(
                self._cancellable_query, llm, prompt, [], llm_model, False, sid, web_search
            )

        async def _call_with_heartbeat(prompt: str, start: float, cap: float, message: str) -> str:
            """长耗时模型调用期间按固定间隔推进进度（心跳实现见基类，与分镜阶段共用）。"""
            return await self._await_with_heartbeat(
                _call(prompt), start=start, cap=cap, message=message, log=_log,
            )

        parsed: Optional[dict] = None
        _log(10, "正在构建修订提示词...")
        if scope == "episode":
            template = _get_script_prompt(
                "revise_episode_by_instruction", "zh" if is_zh else "en"
            )
            prompt = template.format(
                instruction=instruction,
                target_episode=script_revision.build_target_episode_block(target_episode),
                other_episodes_brief=script_revision.build_episode_brief(episodes, target_number),
                characters=json.dumps(current.get("characters") or [], ensure_ascii=False),
                settings=json.dumps(current.get("settings") or [], ensure_ascii=False),
                film_type=film_type,
                subject_mode=subject_mode,
            )
            _log(20, "正在请求模型修订目标集...")
            for attempt in range(2):
                raw = await _call_with_heartbeat(
                    prompt,
                    20 if attempt == 0 else 82,
                    78 if attempt == 0 else 88,
                    "模型正在生成修订稿，请稍候...",
                )
                _log(80 if attempt == 0 else 88, "正在解析修订结果...")
                candidate = self._extract_json_from_text(raw)
                if isinstance(candidate, dict) and isinstance(candidate.get("episode"), dict):
                    parsed = candidate
                    break
                logger.warning("[ScriptWriter] 单集修订解析失败 attempt=%s", attempt + 1)
                _log(82 if attempt == 0 else 90, f"修订结果解析失败，重试 {attempt + 1}/2")
            if parsed is None:
                raise ValueError("修订结果解析失败，请重试")
            _log(92, "正在合并单集修订结果...")
            new_episodes = script_revision.merge_single_episode(episodes, parsed["episode"])
            script_revision.assert_untouched_episodes(episodes, new_episodes, target_number)
            # 单集修订不动角色/场景，但产物本身可能已经缺 ID（旧版全剧修订遗留）：
            # 先复制一份再补齐，不能改 current（base_version 以修订前正文为准）。
            new_characters = copy.deepcopy(current.get("characters") or [])
            new_settings = copy.deepcopy(current.get("settings") or [])
            script_revision.ensure_item_ids(new_characters, new_characters, "character_id", "char")
            script_revision.ensure_item_ids(new_settings, new_settings, "setting_id", "set")
            revised = next(
                ep for ep in new_episodes
                if int(ep.get("episode_number", ep.get("act_number", 0))) == target_number
            )
            preview_body = {"episode": revised}
        else:
            template = _get_script_prompt(
                "revise_script_by_instruction", "zh" if is_zh else "en"
            )
            prompt = template.format(
                instruction=instruction,
                episodes=json.dumps(episodes, ensure_ascii=False),
                characters=json.dumps(current.get("characters") or [], ensure_ascii=False),
                settings=json.dumps(current.get("settings") or [], ensure_ascii=False),
                film_type=film_type,
                subject_mode=subject_mode,
            )
            _log(20, "正在请求模型修订全剧...")
            for attempt in range(2):
                raw = await _call_with_heartbeat(
                    prompt,
                    20 if attempt == 0 else 82,
                    78 if attempt == 0 else 88,
                    "模型正在生成修订稿，请稍候...",
                )
                _log(80 if attempt == 0 else 88, "正在解析修订结果...")
                candidate = self._extract_json_from_text(raw)
                if isinstance(candidate, dict) and isinstance(candidate.get("episodes"), list):
                    try:
                        new_episodes = script_revision.validate_episode_numbers(
                            episodes, candidate["episodes"]
                        )
                    except ValueError as exc:
                        logger.warning("[ScriptWriter] 全剧修订集号校验失败: %s", exc)
                        _log(82 if attempt == 0 else 90, f"集号校验失败，重试 {attempt + 1}/2")
                        continue
                    parsed = candidate
                    break
                _log(82 if attempt == 0 else 90, f"修订结果解析失败，重试 {attempt + 1}/2")
            if parsed is None:
                raise ValueError("修订结果集号与原文不一致，已停止本次修订")
            _log(92, "正在整理修订后的剧集...")
            new_characters = copy.deepcopy(
                parsed.get("characters")
                if isinstance(parsed.get("characters"), list)
                else (current.get("characters") or [])
            )
            new_settings = copy.deepcopy(
                parsed.get("settings")
                if isinstance(parsed.get("settings"), list)
                else (current.get("settings") or [])
            )
            # 模型输出的角色/场景不带 character_id/setting_id：原样落入预览会在应用后把产物主键
            # 抹掉，阶段 2 按 ID 取数会整段丢条目（“未能从剧本中读取到角色或场景描述数据”）。
            # 同名先复用修订前 ID（保住选中/版本/资产），新增条目按名称派生补齐并随预览回写产物。
            new_characters = script_revision.ensure_item_ids(
                current.get("characters") or [], new_characters, "character_id", "char"
            )
            new_settings = script_revision.ensure_item_ids(
                current.get("settings") or [], new_settings, "setting_id", "set"
            )
            preview_body = {"episodes": new_episodes}

        _log(96, "正在比对集间差异与下游影响...")
        char_art = self._session_artifact(input_data, "character_design")
        storyboard_art = self._session_artifact(input_data, "storyboard")
        diff_numbers = script_revision.diff_episode_numbers(episodes, new_episodes)
        preview = {
            "instruction": instruction,
            "scope": scope,
            "episode_number": target_number,
            "summary": str(parsed.get("summary") or "").strip(),
            "base_version": script_revision.content_fingerprint(current),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "diff_episode_numbers": diff_numbers,
            "stale_storyboards": script_revision.stale_storyboards(diff_numbers, storyboard_art),
            "removed_items": (
                script_revision.removed_items(
                    current.get("characters") or [], new_characters, char_art.get("characters")
                )
                + script_revision.removed_items(
                    current.get("settings") or [], new_settings, char_art.get("settings"), id_key="setting_id"
                )
            ),
            "user_edited_items": (
                script_revision.user_edited_items(
                    current.get("characters") or [], new_characters, char_art.get("characters"), kind="character"
                )
                + script_revision.user_edited_items(
                    current.get("settings") or [], new_settings, char_art.get("settings"), kind="setting"
                )
            ),
            "duplicate_names": (
                script_revision.duplicate_names(new_characters)
                + script_revision.duplicate_names(new_settings)
            ),
            "characters": new_characters,
            "settings": new_settings,
        }
        preview.update(preview_body)

        payload = copy.deepcopy(current)
        payload.pop("pending_review", None)
        payload["pending_revision"] = preview
        _log(100, "修订预览已生成")
        return {"payload": payload, "requires_intervention": True, "stage_completed": False}

    async def _run_apply_revision(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """应用预览：纯落盘，不再调用 LLM。"""
        floor = [0.0]

        def _log(percent: float, message: str):
            floor[0] = max(floor[0], float(percent))
            self._report_progress("AI 修改", message, floor[0])

        _log(10, "正在读取待应用的修订预览...")
        current = self._session_artifact(input_data, "script_generation")
        if not current:
            raise ValueError("剧本产物不存在，无法应用修订")
        preview = current.get("pending_revision")
        if not isinstance(preview, dict):
            # 编排器物化按 PREVIEW_KEYS 剥离预览（split_preview），_session_artifacts
            # 里没有 pending_revision；干预入口的顶层快照仍带预览，从这里回退读取。
            fallback_preview = input_data.get("pending_revision")
            preview = fallback_preview if isinstance(fallback_preview, dict) else None
        if not isinstance(preview, dict):
            raise ValueError("没有待应用的修订预览")
        _log(30, "正在校验预览是否仍然有效...")
        if preview.get("base_version") != script_revision.content_fingerprint(current):
            raise ValueError("预览已过期或产物已变化，请重新发起修订")
        if preview.get("removed_items") and not intervention.get("confirm_removals"):
            raise ValueError("存在将被删除的角色或场景，请确认后再应用")
        # R13：预览时的 user_edited_items 是快照；应用前必须用「当前」阶段 2 重新比对。
        _log(50, "正在比对角色/场景的手改冲突...")
        char_art = self._session_artifact(input_data, "character_design") or {}
        current_overwrites = (
            script_revision.user_edited_items(
                current.get("characters") or [],
                preview.get("characters") or [],
                char_art.get("characters"),
                kind="character",
            )
            + script_revision.user_edited_items(
                current.get("settings") or [],
                preview.get("settings") or [],
                char_art.get("settings"),
                kind="setting",
            )
        )
        displayed = [item for item in (preview.get("user_edited_items") or []) if isinstance(item, dict)]

        def _conflict_signature(items):
            # 角色与场景可能同名：签名必须带类型
            return sorted(
                (
                    str(item.get("kind") or ""),
                    str(item.get("name") or ""),
                    str(item.get("current_description") or ""),
                )
                for item in items
            )

        # 以 (kind, name) 为键合并：不能只按 name，否则同名角色/场景互相覆盖；
        # 旧的无类型预览条目在同类名有新冲突时被取代（随后签名不一致会强制重新确认）。
        typed_names = {
            (str(item.get("kind") or ""), str(item.get("name") or ""))
            for item in current_overwrites
        }
        merged: dict = {}
        order: list = []
        for item in displayed:
            kind = str(item.get("kind") or "")
            name = str(item.get("name") or "")
            if not kind and any(conflict_name == name for (_k, conflict_name) in typed_names):
                continue
            key = (kind, name)
            if key not in merged:
                order.append(key)
            merged[key] = dict(item)
        for item in current_overwrites:
            kind = str(item.get("kind") or "")
            name = str(item.get("name") or "")
            key = (kind, name)
            if key not in merged:
                order.append(key)
            merged[key] = dict(item)
        conflicts = [merged[key] for key in order]

        # confirm_overwrites 只授权“已展示过的那份差异”；差异变了（新增条目或
        # 同名描述变化）就必须重新展示并重新确认，布尔值不能授权后来发生的改动。
        if conflicts and (
            not intervention.get("confirm_overwrites")
            or _conflict_signature(conflicts) != _conflict_signature(displayed)
        ):
            import copy as _copy

            refreshed = _copy.deepcopy(preview)
            refreshed["user_edited_items"] = conflicts
            payload = _copy.deepcopy(current)
            payload["pending_revision"] = refreshed
            _log(100, "存在需要重新确认的覆盖项")
            return {
                "payload": payload,
                "requires_intervention": True,
                "stage_completed": False,
                "revision_needs_confirmation": True,
            }

        _log(70, "正在合并剧本正文...")
        updated = script_revision.apply_preview_to_artifact(
            current,
            preview,
            instruction=str(preview.get("instruction") or ""),
            scope=str(preview.get("scope") or "all"),
            episode_number=preview.get("episode_number"),
        )
        _log(90, "正在保存剧本产物...")
        self._save_result(updated, input_data.get("session_id", ""), True)
        _log(100, "修订已应用")
        return {
            "payload": updated,
            "requires_intervention": False,
            "stage_completed": True,
            "revision_applied": True,
        }

    async def _run_review_script(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """剧本 AI 质检：只产出逐行修改建议预览，不写产物。"""
        import copy

        floor = 0.0

        def _log(percent: float, message: str):
            nonlocal floor
            floor = max(floor, float(percent))
            self._report_progress("剧本AI质检", message, floor)

        _log(5, "正在读取剧本产物...")
        sid = input_data.get("session_id", "")
        current = self._session_artifact(input_data, "script_generation")
        if not current:
            raise ValueError("剧本产物不存在，无法质检")
        current_meta = current.get("meta") if isinstance(current.get("meta"), dict) else {}
        if not current_meta.get("source_format"):
            raise ValueError("剧本AI质检仅支持导入剧本")
        episodes = current.get("episodes") or []
        if not episodes:
            raise ValueError("剧本产物不包含剧集")
        _total_chars, _max_episode_chars = review_episode_size(episodes)

        is_zh = self._session_language(input_data) == "zh"
        llm_model = self._require_input(input_data, "llm_model")
        web_search = input_data.get("web_search", False)

        from models.llm_client import LLM

        llm = LLM()

        async def _call(prompt: str) -> str:
            # 质检建议会写回正文：safe_content=False 禁止 full_to_half 改写原文标点
            return await asyncio.to_thread(
                self._cancellable_query, llm, prompt, [], llm_model, False, sid, web_search,
                timeout=review_timeout,
            )

        _log(10, "正在构建质检提示词...")
        template = _get_script_prompt("review_script", "zh" if is_zh else "en")
        prompt = template.format(
            episodes=script_review.build_numbered_episodes(episodes),
            characters=json.dumps(current.get("characters") or [], ensure_ascii=False),
            settings=json.dumps(current.get("settings") or [], ensure_ascii=False),
        )
        # 超时按实际 prompt 长度（正文+角色/场景/行号/提示词）计算，不再重复叠加正文
        review_timeout = script_review_timeout_for_chars(len(prompt))

        parsed: Optional[dict] = None
        for attempt in range(2):
            _log(20 if attempt == 0 else 82, "正在请求模型质检...")
            raw = await self._await_with_heartbeat(
                _call(prompt),
                start=20 if attempt == 0 else 82,
                cap=78 if attempt == 0 else 88,
                message="模型正在逐行检查剧本，请稍候...",
                log=_log,
            )
            _log(80 if attempt == 0 else 90, "正在解析质检结果...")
            candidate = self._extract_json_from_text(raw)
            if isinstance(candidate, dict):
                normalized = script_review.normalize_review(candidate, episodes)
                if normalized is not None:
                    parsed = normalized
                    break
            logger.warning("[ScriptReview] 质检结果解析失败 attempt=%s", attempt + 1)

        if parsed is None:
            raise ValueError("质检结果解析失败，请重试")

        _log(96, "正在生成质检预览...")
        preview = {
            "summary": parsed.get("summary") or "",
            "issues": parsed.get("issues") or [],
            "base_version": script_revision.content_fingerprint(current),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        payload = copy.deepcopy(current)
        # 修订预览与质检预览互斥：新质检结果出现时，旧修订预览不得继续悬挂
        payload.pop("pending_revision", None)
        payload["pending_review"] = preview
        _log(100, "质检完成，请选择要应用的修改")
        return {
            "payload": payload,
            "requires_intervention": True,
            "stage_completed": False,
        }

    async def _run_apply_script_review(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """应用用户勾选的质检建议：只改正文，不调用 LLM。"""
        floor = 0.0

        def _log(percent: float, message: str):
            nonlocal floor
            floor = max(floor, float(percent))
            self._report_progress("剧本AI质检", message, floor)

        _log(10, "正在读取质检结果...")
        current = self._session_artifact(input_data, "script_generation")
        if not current:
            current = {
                key: value for key, value in input_data.items()
                if not str(key).startswith("_")
            }
        preview = current.get("pending_review")
        if not isinstance(preview, dict):
            # 编排器物化按 PREVIEW_KEYS 剥离预览（split_preview），
            # 干预入口的顶层快照仍带质检预览，从这里回退读取。
            fallback = input_data.get("pending_review")
            preview = fallback if isinstance(fallback, dict) else None
        if not isinstance(preview, dict):
            raise ValueError("没有待应用的质检结果")
        current_meta = current.get("meta") if isinstance(current.get("meta"), dict) else {}
        if not current_meta.get("source_format"):
            raise ValueError("剧本AI质检仅支持导入剧本")

        _log(30, "正在校验质检结果是否仍然有效...")
        if preview.get("base_version") != script_revision.content_fingerprint(current):
            raise ValueError("质检结果已过期或剧本已变化，请重新发起质检")

        issues = script_review.select_issues(preview, intervention.get("selected_issue_ids"))
        if not issues:
            raise ValueError("请至少选择一条质检建议")

        _log(60, "正在应用所选质检建议...")
        # 最小快照：只保留正文与派生字段，避免把整份 artifact 再复制一遍；
        # 应用历史仍在 current.revision_history 中，撤销时追加一条后保留完整审计链。
        rollback_snapshot = {
            "episodes": copy.deepcopy(current.get("episodes") or []),
            "summary": current.get("summary") or "",
            "before_version": preview.get("base_version") or script_revision.content_fingerprint(current),
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "issue_ids": [str(issue.get("id")) for issue in issues],
        }
        for key in ("line_annotations", "scene_spans", "import_review"):
            if key in current:
                rollback_snapshot[key] = copy.deepcopy(current[key])
        new_episodes = script_review.apply_issue_edits(
            current.get("episodes") or [], issues
        )

        _log(80, "正在保存剧本产物...")
        summary = str(preview.get("summary") or "").strip()
        updated = script_revision.apply_preview_to_artifact(
            current,
            {"episodes": new_episodes, "summary": summary},
            instruction="剧本AI质检：" + (summary or f"应用 {len(issues)} 条建议"),
            scope="all",
            episode_number=None,
        )
        updated.pop("pending_review", None)
        updated.pop("review_rollback", None)
        # 撤销校验只看用户可编辑内容，忽略服务端回写的 line_start/end/主键等派生字段
        rollback_snapshot["after_version"] = script_revision.structure_fingerprint(updated)
        rollback_snapshot["after_aux_version"] = _review_aux_fingerprint(updated)
        updated["review_rollback"] = rollback_snapshot
        _log(100, "质检修改已应用")
        return {
            "payload": updated,
            "requires_intervention": False,
            "stage_completed": True,
            "revision_applied": True,
        }

    async def _run_rollback_script_review(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """撤销上一次剧本 AI 质检写入：恢复质检前正文快照，不调用 LLM。"""
        current = self._session_artifact(input_data, "script_generation")
        if not current:
            current = {
                key: value for key, value in input_data.items()
                if not str(key).startswith("_")
            }
        rollback = current.get("review_rollback")
        if not isinstance(rollback, dict):
            raise ValueError("没有可撤销的剧本AI质检")
        if rollback.get("after_version") != script_revision.structure_fingerprint(current):
            raise ValueError("剧本已再次修改，无法自动撤销本次质检")
        if rollback.get("after_aux_version") != _review_aux_fingerprint(current):
            raise ValueError("剧本摘要/标注已修改，无法自动撤销本次质检")
        episodes = rollback.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            raise ValueError("质检撤销快照无效")
        updated = copy.deepcopy(current)
        updated["episodes"] = copy.deepcopy(episodes)
        updated["summary"] = str(rollback.get("summary") or "")
        for key in ("line_annotations", "scene_spans", "import_review"):
            if key in rollback:
                updated[key] = copy.deepcopy(rollback[key])
            else:
                updated.pop(key, None)
        history = updated.get("revision_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "instruction": "撤销剧本AI质检",
                "scope": "all",
                "episode_number": None,
                "summary": "恢复质检前正文",
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "base_version": rollback.get("after_version", ""),
            }
        )
        updated["revision_history"] = history[-script_revision.REVISION_HISTORY_LIMIT:]
        updated.pop("review_rollback", None)
        updated.pop("pending_review", None)
        updated.pop("pending_revision", None)
        return {
            "payload": updated,
            "requires_intervention": False,
            "stage_completed": True,
            "revision_applied": True,
        }

    @staticmethod
    def _resolve_uploaded_document(file_path: str, owner_login: str) -> str:
        """复用上传目录守卫；越界、跨 owner 或缺失直接拒绝。"""
        from api.media_paths import resolve_uploaded_document
        from core.identity import owner_key

        # 消费入口必须显式携带会话 owner，不能只验证“位于上传目录”
        full_path = resolve_uploaded_document(file_path, owner_key(owner_login))
        if not os.path.exists(full_path):
            raise ValueError("上传文件不存在，请重新上传")
        return full_path

    @staticmethod
    def _scene_blocks(lines: List[str], candidates: List[dict]) -> str:
        headings = [item for item in candidates if item["level"] in ("act", "scene")]
        blocks: List[str] = []
        for index, heading in enumerate(headings):
            end = headings[index + 1]["line"] if index + 1 < len(headings) else len(lines)
            body = [line.strip()[:160] for line in lines[heading["line"] + 1:end] if line.strip()][:8]
            blocks.append(
                f"行{heading['line']} 标题：{heading['title']}\n画面描述："
                + ("\n".join(body) or "（无）")
            )
        return "\n\n".join(blocks)

    async def _run_import_script(self, input_data: Dict[str, Any]) -> Dict:
        """保真导入：只做结构解析、角色/场景与元信息提取，不调用生成/评估/修订/删减。"""
        from models.file_reader import FileReader
        from models.llm_client import LLM

        sid = input_data.get("session_id", "")
        session_meta = self._session_meta(input_data)
        file_path = str(session_meta.get("file_path") or input_data.get("file_path") or "").strip()
        if not file_path:
            raise ValueError("导入剧本缺少上传文件")
        owner_login = str(input_data.get("_session_owner") or "").strip()
        if not owner_login:
            raise ValueError("会话缺少归属信息，无法读取上传剧本")
        full_path = self._resolve_uploaded_document(file_path, owner_login)

        self._report_progress("剧本导入", "正在读取上传剧本...", 10)
        raw_text = FileReader.extract_text(full_path)
        if not str(raw_text or "").strip():
            raise ValueError("上传剧本内容为空")

        lines = script_import.split_lines(raw_text)
        candidates = script_import.detect_candidates(lines)
        lang = self._session_language(input_data, body=raw_text)
        is_zh = lang == "zh"
        llm_model = self._require_input(input_data, "llm_model")
        web_search = input_data.get("web_search", False)
        llm = LLM()
        loop = asyncio.get_running_loop()

        async def _call(prompt: str) -> str:
            return await asyncio.to_thread(
                self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search
            )

        # L2：仅在候选歧义时让模型裁决集级分段点，不接触正文
        llm_starts = None
        if script_import.needs_llm_decision(candidates):
            self._report_progress("剧本导入", "正在判定分集边界...", 30)
            candidate_text = "\n".join(
                f'{item["line"]}: {item["title"][:40]} [{item["level"]}]'
                for item in candidates[:200]
            )
            try:
                prompt = _get_script_prompt("structure_probe", lang).format(candidates=candidate_text)
                parsed = self._extract_json_from_text(await _call(prompt))
                if isinstance(parsed, dict) and isinstance(parsed.get("episode_start_lines"), list):
                    llm_starts = [
                        int(value)
                        for value in parsed["episode_start_lines"]
                        if str(value).lstrip("-").isdigit()
                    ] or None
            except Exception:
                logger.warning("[ScriptImport] 结构裁决失败，回退确定性规则", exc_info=True)

        # 场景抽取：只传幕/场标题与画面描述片段
        self._report_progress("剧本导入", "正在抽取场景...", 50)
        inferred_settings: List[dict] = []
        heading_mapping: Dict[int, str] = {}
        scene_blocks = self._scene_blocks(lines, candidates)
        if scene_blocks:
            try:
                prompt = _get_script_prompt("scene_extract", lang).format(scene_blocks=scene_blocks)
                parsed = self._extract_json_from_text(await _call(prompt))
                if isinstance(parsed, dict) and isinstance(parsed.get("settings"), list):
                    candidate_lines = {item["line"] for item in candidates}
                    for item in parsed["settings"]:
                        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                            continue
                        inferred_settings.append(item)
                        for line_no in item.get("heading_lines") or []:
                            try:
                                value = int(line_no)
                            except (TypeError, ValueError):
                                continue
                            if value in candidate_lines:
                                heading_mapping[value] = str(item["name"]).strip()
            except Exception:
                logger.warning("[ScriptImport] 场景抽取失败，保留空场景", exc_info=True)

        # 元信息与角色/场景补全：不覆盖显式角色设定
        self._report_progress("剧本导入", "正在提取角色与元信息...", 70)
        meta_data: Dict[str, Any] = {}
        try:
            meta_text = raw_text if len(raw_text) <= 60000 else raw_text[:60000]
            if len(raw_text) > 60000:
                logger.warning(
                    "[ScriptImport] 剧本 %d 字符，元信息/角色提取仅使用前 60000 字符",
                    len(raw_text),
                )
            prompt = _get_script_prompt("import_meta_extract", lang).format(script_text=meta_text)
            parsed = self._extract_json_from_text(await _call(prompt))
            if isinstance(parsed, dict):
                meta_data = parsed
        except Exception:
            logger.warning("[ScriptImport] 元信息提取失败，使用默认值", exc_info=True)

        # 导入阶段不再执行剧本质检：质检改为剧本阶段的显式「剧本AI质检」，
        # 质检建议只作用于剧本正文，不再为分镜预置行级时长。
        # 四条质量约束（内容/时序、时长、视听节奏、总时长灵活）落在 review_script
        # 与分镜提示词中；导入本身保持保真，不把质量改写规则混入提取提示词。
        inferred_characters = meta_data.get("characters") if isinstance(meta_data.get("characters"), list) else []
        meta_settings = meta_data.get("settings") if isinstance(meta_data.get("settings"), list) else []
        existing_names = {
            str(item.get("name") or "").strip()
            for item in inferred_settings
            if isinstance(item, dict)
        }
        for item in meta_settings:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            if str(item["name"]).strip() in existing_names:
                continue
            inferred_settings.append(item)
            existing_names.add(str(item["name"]).strip())

        film_type = self._normalize_film_type(meta_data.get("film_type")) or DEFAULT_FILM_TYPE
        subject_mode = self._normalize_subject_mode(meta_data.get("subject_mode")) or DEFAULT_SUBJECT_MODE

        self._report_progress("剧本导入", "正在组装结构化剧本...", 90)
        artifact = script_import.build_import_artifact(
            raw_text,
            session_id=sid,
            title=str(meta_data.get("title") or "").strip(),
            logline=str(meta_data.get("logline") or "").strip(),
            genre=meta_data.get("genre") if isinstance(meta_data.get("genre"), list) else [],
            mood=str(meta_data.get("mood") or "").strip(),
            film_type=film_type,
            subject_mode=subject_mode,
            llm_start_lines=llm_starts,
            inferred_characters=inferred_characters,
            inferred_settings=inferred_settings,
            setting_by_heading_line=heading_mapping,
        )
        meta = artifact.setdefault("meta", {})
        # 导入剧本以内容完整与节奏舒适优先：原文目标时长只作提示，不再硬压分镜
        meta["duration_budget"] = "advisory"
        artifact.pop("source_episode_count", None)
        self._save_result(artifact, sid, is_zh)
        self._report_progress("剧本导入", "剧本解析完成", 100)
        return {
            "payload": artifact,
            "requires_intervention": False,
            "stage_completed": True,
            "source_episode_count": len(artifact.get("episodes") or []),
        }

    @classmethod
    def _split_episode_blocks(cls, script_text: str) -> List[dict]:
        episode_re = re.compile(
            r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:第\s*(\d+)\s*集(?![-－])|Episode\s+(\d+)\b)",
            re.IGNORECASE,
        )
        blocks: List[dict] = []
        current = {"episode_number": 1, "lines": []}
        seen_header = False

        for line in script_text.splitlines():
            match = episode_re.match(line.strip())
            if match:
                if seen_header and current["lines"]:
                    blocks.append(current)
                ep_no = int(match.group(1) or match.group(2) or len(blocks) + 1)
                current = {"episode_number": ep_no, "lines": [line]}
                seen_header = True
            else:
                current["lines"].append(line)

        if current["lines"]:
            blocks.append(current)
        return blocks

    @staticmethod
    def _is_counted_script_line(line: str) -> bool:
        text = line.strip()
        if not text:
            return False
        if re.match(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:第\s*\d+\s*集(?![-－])|Episode\s+\d+\b)", text, re.IGNORECASE):
            return False
        if re.match(r"^\s*(?:\*\*)?\s*(?:第\s*\d+\s*集[-－]第\s*\d+\s*场|\d+\s*[-－]\s*\d+\b)", text, re.IGNORECASE):
            return False
        if text.startswith("**") and text.endswith("**"):
            return False
        if re.match(r"^(人物|角色|场景元素|Characters|Scene elements)\s*[:：]", text, re.IGNORECASE):
            return False
        return True

    @classmethod
    def _script_length_stats(cls, script_text: str) -> List[dict]:
        stats: List[dict] = []
        for block in cls._split_episode_blocks(script_text):
            counted = [line for line in block["lines"] if cls._is_counted_script_line(line)]
            stats.append({
                "episode_number": block["episode_number"],
                "line_count": len(counted),
                "too_long": len(counted) > cls.MAX_EPISODE_LINES,
            })
        return stats

    @classmethod
    def _length_feedback(cls, stats: List[dict]) -> str:
        return "\n".join(
            f"- 第{item['episode_number']}集：{item['line_count']}行"
            for item in stats
        )

    @staticmethod
    def _expected_episode_numbers(episodes: int) -> List[int]:
        return list(range(1, max(1, int(episodes)) + 1))

    @classmethod
    def _episode_numbers_from_script(cls, script_text: str) -> List[int]:
        return [
            int(block["episode_number"])
            for block in cls._split_episode_blocks(script_text)
            if block.get("lines")
        ]

    @classmethod
    def _episode_count_feedback(cls, script_text: str, episodes: int) -> str:
        expected = cls._expected_episode_numbers(episodes)
        found = cls._episode_numbers_from_script(script_text)
        missing = [num for num in expected if num not in found]
        extra = [num for num in found if num not in expected]
        return (
            f"目标集数：{episodes}；应包含集号：{expected}；"
            f"当前识别到集号：{found or '无'}；缺失：{missing or '无'}；多余：{extra or '无'}。"
        )

    @classmethod
    def _episode_count_matches(cls, script_text: str, episodes: int) -> bool:
        found = sorted(set(cls._episode_numbers_from_script(script_text)))
        return found == cls._expected_episode_numbers(episodes)

    @classmethod
    def _build_episodes_from_script_text(cls, script_text: str, expected_episodes: int) -> List[dict]:
        episodes: List[dict] = []
        for block in cls._split_episode_blocks(script_text):
            ep_no = int(block.get("episode_number") or len(episodes) + 1)
            if ep_no < 1 or ep_no > expected_episodes:
                continue
            content = "\n".join(block.get("lines") or []).strip()
            if not content:
                continue
            episodes.append({
                "episode_number": ep_no,
                "act_title": f"第{ep_no}集",
                "content": content,
            })
        deduped: Dict[int, dict] = {}
        for ep in episodes:
            deduped[ep["episode_number"]] = ep
        return [deduped[num] for num in cls._expected_episode_numbers(expected_episodes) if num in deduped]

    async def process(self, input_data: Any, intervention: Optional[Dict] = None) -> Dict:
        if str(input_data.get("script_source") or "") == "upload" and not intervention:
            return await self._run_import_script(input_data)
        if intervention and intervention.get("action") == "revise_script":
            return await self._run_revise_script(input_data, intervention)
        if intervention and intervention.get("action") == "apply_revision":
            return await self._run_apply_revision(input_data, intervention)
        if intervention and intervention.get("action") == "review_script":
            return await self._run_review_script(input_data, intervention)
        if intervention and intervention.get("action") == "apply_script_review":
            return await self._run_apply_script_review(input_data, intervention)
        if intervention and intervention.get("action") == "rollback_script_review":
            return await self._run_rollback_script_review(input_data, intervention)

        if intervention and "modified_script" in intervention:
            modified = intervention["modified_script"]
            sid = input_data.get("session_id", "")
            if isinstance(modified, str):
                modified = self._extract_json_from_text(modified) or {}
            modified["session_id"] = sid
            # 手动编辑提交的是完整产物：预览字段一律清除，不随产物落盘
            modified.pop("pending_revision", None)
            # 类型缺省回填：保证"类型随产物持久化"对人工干预路径同样成立
            modified_meta = modified.get("meta")
            if not isinstance(modified_meta, dict):
                modified_meta = {}
                modified["meta"] = modified_meta
            if not self._normalize_film_type(modified_meta.get("film_type")):
                modified_meta["film_type"] = DEFAULT_FILM_TYPE
            if not self._normalize_subject_mode(modified_meta.get("subject_mode")):
                modified_meta["subject_mode"] = DEFAULT_SUBJECT_MODE
            # 【优化】移除手动调用 self._save_result，依靠 Orchestrator 自动保存
            return {"payload": modified, "requires_intervention": False, "stage_completed": True}

        # 处理确认续写或删除续写的结果，更新script_genenration和character_design数据结构，并保存最终结果
        if intervention and intervention.get("action") in ["confirm_continue", "delete_continue"]:
            import copy
            final_data = copy.deepcopy(input_data)
            final_data.pop("pending_revision", None)
            sid = final_data.get("session_id", "")
            
            if intervention.get("action") == "confirm_continue":
                new_chars = final_data.get("new_characters", [])
                new_settings = final_data.get("new_settings", [])
                new_ep_list = final_data.get("new_episodes", [])
                
                # 更新第一阶段剧本数据 (内存)
                final_data.setdefault("episodes", []).extend(new_ep_list)
                final_data.setdefault("characters", []).extend(new_chars)
                final_data.setdefault("settings", []).extend(new_settings)

                # 创建一个包含增量信息的返回结果，供 Orchestrator 钩子使用
                result_payload = copy.deepcopy(final_data)
                result_payload["new_characters"] = new_chars
                result_payload["new_settings"] = new_settings
                result_payload["new_episodes"] = new_ep_list

                logger.info("[ScriptWriter] Confirmed continuation. Providing incremental data to Orchestrator.")
                return {"payload": result_payload, "requires_intervention": False, "stage_completed": True}

            # 处理 delete_continue 的情况，直接丢弃新增内容，保持原有剧本数据不变
            for key in ["new_episodes", "new_characters", "new_settings", "sequel_idea", "pending_revision"]:
                final_data.pop(key, None)
            return {"payload": final_data, "requires_intervention": False, "stage_completed": True}
        # ---------------------------------------------------- #

        async def run_smart_continue():
            import copy
            sid = input_data.get("session_id", "")
            llm_model = self._require_input(input_data, "llm_model")
            web_search = input_data.get("web_search", False)
            episodes_to_add = intervention.get("episodes_to_add", 1)
            sequel_idea = intervention.get("sequel_idea", "").strip()
            # 续写沿用原片类型与主体形式：读取产物 meta（缺省默认，兼容旧会话），不重复判定
            existing_meta = input_data.get("meta") if isinstance(input_data.get("meta"), dict) else {}
            film_type = self._normalize_film_type(existing_meta.get("film_type")) or DEFAULT_FILM_TYPE
            subject_mode = self._normalize_subject_mode(existing_meta.get("subject_mode")) or DEFAULT_SUBJECT_MODE

            from models.llm_client import LLM
            llm = LLM()

            def _log_progress(pct, msg):
                self._report_progress("智能续写", msg, pct)
                logger.info(f"[{pct}%] {msg}")

            loop = asyncio.get_running_loop()
            
            existing_episodes_text = json.dumps(input_data.get("episodes", []), ensure_ascii=False)
            existing_chars_text = json.dumps(input_data.get("characters", []), ensure_ascii=False)
            existing_settings_text = json.dumps(input_data.get("settings", []), ensure_ascii=False)
            lang = self._continuation_language(
                input_data, body=existing_episodes_text,
                title=str(self._session_meta(input_data).get("title") or input_data.get("title") or ""),
            )

            last_episode_num = 0
            if input_data.get("episodes"):
                last_episode_num = input_data["episodes"][-1].get("episode_number", len(input_data["episodes"]))

            if not sequel_idea:
                _log_progress(10, "生成续写灵感...")
                if lang == "en":
                    idea_prompt = (
                        f"Based on the existing episodes below, provide a short continuation idea "
                        f"(main direction) for the next {episodes_to_add} episodes in under 100 words: "
                        f"{existing_episodes_text}"
                    )
                else:
                    idea_prompt = f"根据以下已有的剧集内容，在100字内，提供一个后续{episodes_to_add}集的简短续写灵感(主线方向): {existing_episodes_text}"
                sequel_idea = await asyncio.to_thread(self._cancellable_query, llm, idea_prompt, [], llm_model, True, sid, web_search)
                sequel_idea = sequel_idea.strip()

            _log_progress(30, "正在生成续写剧本文本...")
            prompt_name = "smart_continue_script"
            prompt = _get_script_prompt(prompt_name, lang).format(
                episodes_text=existing_episodes_text,
                chars_text=existing_chars_text,
                settings_text=existing_settings_text,
                episodes_to_add=episodes_to_add,
                sequel_idea=sequel_idea,
                start_episode_num=last_episode_num + 1,
                film_type=film_type,
                subject_mode=subject_mode
            )
            
            _log_progress(45, "正在生成续写初稿...")
            sequel_script_text = await asyncio.to_thread(self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search)

            _log_progress(50, "正在评估台词与情节...")
            eval_dialogue_prompt = _get_script_prompt("eval_dialogue", lang).format(script_text=sequel_script_text, film_type=film_type, subject_mode=subject_mode)
            eval_plot_prompt = _get_script_prompt("eval_plot", lang).format(script_text=sequel_script_text, film_type=film_type, subject_mode=subject_mode)
            dialogue_critique, plot_critique = await self._evaluate_pair(
                llm, eval_dialogue_prompt, eval_plot_prompt, llm_model, sid, web_search,
                self._evaluation_concurrency(input_data),
            )
            
            _log_progress(58, "正在根据评估意见优化续写内容...")
            revise_prompt = _get_script_prompt("revise_script", lang).format(
                script_text=sequel_script_text, 
                dialogue_critique=dialogue_critique, 
                plot_critique=plot_critique,
                film_type=film_type,
                subject_mode=subject_mode
            )
            sequel_script_text = await asyncio.to_thread(self._cancellable_query, llm, revise_prompt, [], llm_model, True, sid, web_search)

            _log_progress(60, "提取新增人物/场景...")
            meta_prompt = _get_script_prompt("meta_extract_sequel", lang).format(
                existing_chars=existing_chars_text,
                existing_settings=existing_settings_text,
                sequel_script=sequel_script_text
            )
            meta_raw = await asyncio.to_thread(self._cancellable_query, llm, meta_prompt, [], llm_model, True, sid, web_search)
            meta_res = self._extract_json_from_text(meta_raw)
            meta_data = meta_res if isinstance(meta_res, dict) else {}

            new_chars = meta_data.get("new_characters", [])
            new_settings = meta_data.get("new_settings", [])
            for c in new_chars:
                c["character_id"] = self._gen_id("char")
            for s in new_settings:
                s["setting_id"] = self._gen_id("set")

            _log_progress(80, "结构化续写集数据...")
            extract_prompt = _get_script_prompt("act_extract_sequel", lang).format(
                sequel_script=sequel_script_text,
                start_episode_num=last_episode_num + 1,
                episodes_to_add=episodes_to_add
            )
            
            new_episodes = []
            max_retries = 3
            raw_acts = ""
            for attempt in range(max_retries):
                raw_acts = await asyncio.to_thread(self._cancellable_query, llm, extract_prompt, [], llm_model, True, sid, web_search)
                parsed_acts = self._extract_json_from_text(raw_acts)
                
                new_episodes.clear()
                if isinstance(parsed_acts, list):
                    for act in parsed_acts:
                        if isinstance(act, dict):
                            new_episodes.append({
                                "episode_number": act.get("episode_number"),
                                "act_title": act.get("act_title") or f"第{act.get('episode_number')}集",
                                "content": act.get("content", "")
                            })
                elif isinstance(parsed_acts, dict):
                    act_list = parsed_acts.get("new_episodes") or parsed_acts.get("episodes") or list(parsed_acts.values())[0]
                    if isinstance(act_list, list):
                        for act in act_list:
                            if isinstance(act, dict):
                                new_episodes.append({
                                    "episode_number": act.get("episode_number"),
                                    "act_title": act.get("act_title") or f"第{act.get('episode_number')}集",
                                    "content": act.get("content", "")
                                })
                
                if new_episodes:
                    break
                logger.warning(f"[ScriptWriter] Extraction failed on attempt {attempt+1}, retrying...")
                _log_progress(85, f"数据解析失败，自动进行第 {attempt+1} 次重试...")

            # 最终兜底：如果重试多次依然失败，直接将返回的文本全塞进一集里
            if not new_episodes and sequel_script_text:
                logger.error(f"[ScriptWriter] All {max_retries} attempts to parse new episodes failed.")
                new_episodes.append({
                    "episode_number": last_episode_num + 1,
                    "act_title": f"第{last_episode_num + 1}集 续集",
                    "content": sequel_script_text.strip()
                })

            final_data = copy.deepcopy(input_data)
            final_data.pop("pending_revision", None)
            final_data["new_episodes"] = new_episodes
            final_data["new_characters"] = new_chars
            final_data["new_settings"] = new_settings
            final_data["sequel_idea"] = sequel_idea
            
            self._save_result(final_data, sid, lang == "zh")
            _log_progress(100, "智能续写完成")
            return final_data

        if intervention and intervention.get("action") == "smart_continue":
            result = await run_smart_continue()
            # 设置 requires_intervention=True 以触发表单确认按钮
            return {"payload": result, "requires_intervention": True, "stage_completed": False}

        async def run_logic():
            idea = input_data.get("idea", "")
            sid = input_data.get("session_id", "")
            style = input_data.get("style", "anime")
            llm_model = self._require_input(input_data, "llm_model")
            web_search = input_data.get("web_search", False)
            episodes = input_data.get("episodes")
            if episodes is None:
                logger.warning("[ScriptWriter] episodes missing from input_data; falling back to 4. session=%s", sid)
                episodes = 4
            try:
                episodes = max(1, int(episodes))
            except (TypeError, ValueError):
                logger.warning("[ScriptWriter] invalid episodes=%r; falling back to 4. session=%s", episodes, sid)
                episodes = 4
            is_zh = self._session_language(input_data, body=idea) == "zh"

            from config import settings as app_settings
            from models.llm_client import LLM
            os.makedirs(app_settings.TEMP_DIR, exist_ok=True)
            llm = LLM()

            def _log_progress(pct, msg):
                self._report_progress("剧本生成", msg, pct)
                logger.info(f"[{pct}%] {msg}")

            loop = asyncio.get_running_loop()

            # 1. 判定片子类型与主体形式：独立提示词 + 单次 LLM 调用，
            #    解析失败或无法归类时回退默认值，不中断生成流程
            film_type = DEFAULT_FILM_TYPE
            subject_mode = DEFAULT_SUBJECT_MODE
            try:
                classify_prompt = _get_script_prompt("classify_film_type", "zh" if is_zh else "en").format(idea=idea)
                classify_raw = await asyncio.to_thread(self._cancellable_query, llm, classify_prompt, [], llm_model, True, sid, web_search)
                parsed_type, parsed_mode = self._parse_classification(classify_raw)
                film_type = parsed_type or DEFAULT_FILM_TYPE
                subject_mode = parsed_mode or DEFAULT_SUBJECT_MODE
                logger.info(
                    "[ScriptWriter] Film classified as type=%s subject_mode=%s (raw snippet: %.80s)",
                    film_type,
                    subject_mode,
                    (classify_raw or "").replace("\n", " "),
                )
            except Exception:
                logger.warning(
                    "[ScriptWriter] Film classification failed; falling back to %s/%s",
                    DEFAULT_FILM_TYPE,
                    DEFAULT_SUBJECT_MODE,
                    exc_info=True,
                )

            async def _trim_script_if_needed(script_text: str, phase: str) -> str:
                trimmed = script_text
                for attempt in range(2):
                    stats = self._script_length_stats(trimmed)
                    overlong = [item for item in stats if item.get("too_long")]
                    if not overlong:
                        return trimmed
                    logger.warning(
                        "[ScriptWriter] %s script is too long; trimming attempt=%d stats=%s",
                        phase,
                        attempt + 1,
                        stats,
                    )
                    _log_progress(12 if phase == "初稿" else 45, f"{phase}篇幅超限，正在删减到每集{self.MAX_EPISODE_LINES}行以内...")
                    trim_prompt = _get_script_prompt("trim_script", "zh" if is_zh else "en").format(
                        script_text=trimmed,
                        min_lines=self.MIN_EPISODE_LINES,
                        max_lines=self.MAX_EPISODE_LINES,
                        line_report=self._length_feedback(stats),
                        film_type=film_type,
                        subject_mode=subject_mode,
                    )
                    trimmed = await asyncio.to_thread(self._cancellable_query, llm, trim_prompt, [], llm_model, True, sid, web_search)
                    logger.info("[ScriptWriter] Trimmed %s script generated (%d chars)", phase, len(trimmed))
                return trimmed

            async def _repair_episode_count_if_needed(script_text: str, phase: str) -> str:
                repaired = script_text
                for attempt in range(2):
                    if self._episode_count_matches(repaired, episodes):
                        return repaired
                    feedback = self._episode_count_feedback(repaired, episodes)
                    logger.warning(
                        "[ScriptWriter] %s script episode count mismatch; repair attempt=%d %s",
                        phase,
                        attempt + 1,
                        feedback,
                    )
                    _log_progress(14 if phase == "初稿" else 48, f"{phase}集数不一致，正在修正为{episodes}集...")
                    repair_prompt = _get_script_prompt("repair_episode_count", "zh" if is_zh else "en").format(
                        script_text=repaired,
                        episodes=episodes,
                        episode_report=feedback,
                        min_lines=self.MIN_EPISODE_LINES,
                        max_lines=self.MAX_EPISODE_LINES,
                        film_type=film_type,
                        subject_mode=subject_mode,
                    )
                    repaired = await asyncio.to_thread(self._cancellable_query, llm, repair_prompt, [], llm_model, True, sid, web_search)
                    repaired = await _trim_script_if_needed(repaired, f"{phase}集数修复后")
                return repaired

            # 1. Generate full script
            _log_progress(10, "正在生成完整剧本文本初稿...")
            prompt = _get_script_prompt("generate_script", "zh" if is_zh else "en").format(idea=idea, style=style, episodes=episodes, film_type=film_type, subject_mode=subject_mode)

            full_script_text = await asyncio.to_thread(self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search)
            logger.info(f"[ScriptWriter] Initial script generated ({len(full_script_text)} chars)")
            full_script_text = await _trim_script_if_needed(full_script_text, "初稿")
            full_script_text = await _repair_episode_count_if_needed(full_script_text, "初稿")
            
            _log_progress(20, "正在评估台词与情节...")
            eval_dialogue_prompt = _get_script_prompt("eval_dialogue", "zh" if is_zh else "en").format(script_text=full_script_text, film_type=film_type, subject_mode=subject_mode)
            eval_plot_prompt = _get_script_prompt("eval_plot", "zh" if is_zh else "en").format(script_text=full_script_text, film_type=film_type, subject_mode=subject_mode)
            dialogue_critique, plot_critique = await self._evaluate_pair(
                llm, eval_dialogue_prompt, eval_plot_prompt, llm_model, sid, web_search,
                self._evaluation_concurrency(input_data),
            )
            
            _log_progress(40, "正在根据评估意见优化剧本...")
            revise_prompt = _get_script_prompt("revise_script", "zh" if is_zh else "en").format(
                script_text=full_script_text, 
                dialogue_critique=dialogue_critique, 
                plot_critique=plot_critique,
                film_type=film_type,
                subject_mode=subject_mode
            )
            full_script_text = await asyncio.to_thread(self._cancellable_query, llm, revise_prompt, [], llm_model, True, sid, web_search)
            logger.info(f"[ScriptWriter] Final script generated ({len(full_script_text)} chars)")
            full_script_text = await _trim_script_if_needed(full_script_text, "优化后")
            full_script_text = await _repair_episode_count_if_needed(full_script_text, "优化后")

            _log_progress(60, "最终剧本生成完成，正在提取人物/场景信息...")

            # 2. Extract meta data -> total_episodes, characters, settings
            meta_prompt = _get_script_prompt("meta_extract", "zh" if is_zh else "en").format(script_text=full_script_text, outline=full_script_text)
            meta_raw = await asyncio.to_thread(self._cancellable_query, llm, meta_prompt, [], llm_model, True, sid, web_search)
            meta_res = self._extract_json_from_text(meta_raw)
            meta_data = meta_res if isinstance(meta_res, dict) else {}

            # meta_extract 输出的 film_type/subject_mode 仅作一致性交叉校验，判定结果为权威来源，不覆盖
            extracted_film_type = self._normalize_film_type(meta_data.get("film_type"))
            if extracted_film_type and extracted_film_type != film_type:
                logger.warning(
                    "[ScriptWriter] meta_extract film_type=%s inconsistent with classified %s; keeping classified value",
                    extracted_film_type,
                    film_type,
                )
            extracted_subject_mode = self._normalize_subject_mode(meta_data.get("subject_mode"))
            if extracted_subject_mode and extracted_subject_mode != subject_mode:
                logger.warning(
                    "[ScriptWriter] meta_extract subject_mode=%s inconsistent with classified %s; keeping classified value",
                    extracted_subject_mode,
                    subject_mode,
                )

            all_characters = meta_data.get("characters", [])
            all_settings = meta_data.get("settings", [])
            for c in all_characters:
                c["character_id"] = c.get("character_id") or self._gen_id("char")
            for s in all_settings:
                s["setting_id"] = s.get("setting_id") or self._gen_id("set")

            # 3. 解析各集数据 - 针对新版数组输出格式进行优化
            _log_progress(80, "开始结构化全集数据...")
            
            extract_prompt = _get_script_prompt("act_extract", "zh" if is_zh else "en").format(
                script_text=full_script_text,
                episodes=episodes,
            )
            
            raw_acts = await asyncio.to_thread(self._cancellable_query, llm, extract_prompt, [], llm_model, True, sid, web_search)
            parsed_acts = self._extract_json_from_text(raw_acts)
            
            all_episodes = []
            if isinstance(parsed_acts, list):
                for act in parsed_acts:
                    if isinstance(act, dict):
                        all_episodes.append({
                            "episode_number": act.get("episode_number"),
                            "act_title": act.get("act_title") or f"第{act.get('episode_number')}集",
                            "content": act.get("content", "")
                        })

            if not all_episodes:
                logger.error(f"[ScriptWriter] Failed to parse episodes from LLM output. Raw: {raw_acts[:200]}...")
            expected_numbers = self._expected_episode_numbers(episodes)
            parsed_numbers = sorted({
                int(ep.get("episode_number"))
                for ep in all_episodes
                if isinstance(ep.get("episode_number"), int) or str(ep.get("episode_number", "")).isdigit()
            })
            if parsed_numbers != expected_numbers:
                logger.warning(
                    "[ScriptWriter] Structured episodes mismatch; expected=%s parsed=%s. Falling back to regex split.",
                    expected_numbers,
                    parsed_numbers,
                )
                fallback_episodes = self._build_episodes_from_script_text(full_script_text, episodes)
                fallback_numbers = [ep["episode_number"] for ep in fallback_episodes]
                if fallback_numbers == expected_numbers:
                    all_episodes = fallback_episodes
                else:
                    logger.error(
                        "[ScriptWriter] Regex split still mismatched; expected=%s fallback=%s. Keeping parsed output.",
                        expected_numbers,
                        fallback_numbers,
                    )
            
            final_json = {
                "project_id": f"proj_{sid}",
                "session_id": sid,
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "meta": {
                    "generation_model": llm_model,
                    "generation_prompt": idea,
                    "original_text": full_script_text,
                    "film_type": film_type,
                    "subject_mode": subject_mode
                },
                "title": meta_data.get("title", "Generated Script"),
                "logline": meta_data.get("logline", ""),
                "genre": meta_data.get("genre", []),
                "mood": meta_data.get("mood", ""),
                "characters": all_characters,
                "settings": all_settings,
                "episodes": all_episodes
            }
            
            self._save_result(final_json, sid, is_zh)
            _log_progress(100, "剧本结构化解析完成！")
            return final_json

        result = await run_logic()
        return {"payload": result, "requires_intervention": False, "stage_completed": True}
