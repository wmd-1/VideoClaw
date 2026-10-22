# -*- coding: utf-8 -*-
"""
阶段1: 编剧智能体 (直出一遍过版本)
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from prompts.loader import load_prompt_with_fallback

from .. import script_revision
from .base_agent import AgentInterface

logger = logging.getLogger(__name__)

def _get_script_prompt(name: str, lang: str = "zh") -> str:
    return load_prompt_with_fallback("script", name, lang, "zh")


# 全剧修订的上下文字符上限：超出时要求改用单集修订，不静默截断
SCRIPT_REVISION_MAX_CHARS = 60000

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

    def _save_result(self, json_data: dict, sid: str, is_zh: bool):
        from config import settings as app_settings
        os.makedirs(os.path.join(app_settings.RESULT_DIR, 'script'), exist_ok=True)
        out_path = os.path.join(app_settings.RESULT_DIR, 'script', f'{sid}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        logger.info(f"[ScriptWriter] script saved to {out_path}")

    def _save_progress(self, sid: str, phase: str, data: dict):
        pass

    async def _run_revise_script(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """按指令修订剧本，只产出预览，不写产物。"""
        import copy

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
        is_zh = any("\u4e00" <= char <= "\u9fff" for char in instruction)
        llm_model = self._require_input(input_data, "llm_model")
        web_search = input_data.get("web_search", False)

        from models.llm_client import LLM

        llm = LLM()
        loop = asyncio.get_running_loop()

        def _log(percent: float, message: str):
            self._report_progress("AI 修改", message, percent)

        async def _call(prompt: str) -> str:
            return await loop.run_in_executor(
                None, self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search
            )

        parsed: Optional[dict] = None
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
            _log(20, "正在修订目标集...")
            for attempt in range(2):
                raw = await _call(prompt)
                candidate = self._extract_json_from_text(raw)
                if isinstance(candidate, dict) and isinstance(candidate.get("episode"), dict):
                    parsed = candidate
                    break
                logger.warning("[ScriptWriter] 单集修订解析失败 attempt=%s", attempt + 1)
                _log(40, f"修订结果解析失败，重试 {attempt + 1}/2")
            if parsed is None:
                raise ValueError("修订结果解析失败，请重试")
            new_episodes = script_revision.merge_single_episode(episodes, parsed["episode"])
            script_revision.assert_untouched_episodes(episodes, new_episodes, target_number)
            new_characters = current.get("characters") or []
            new_settings = current.get("settings") or []
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
            _log(20, "正在修订全剧...")
            for attempt in range(2):
                raw = await _call(prompt)
                candidate = self._extract_json_from_text(raw)
                if isinstance(candidate, dict) and isinstance(candidate.get("episodes"), list):
                    try:
                        new_episodes = script_revision.validate_episode_numbers(
                            episodes, candidate["episodes"]
                        )
                    except ValueError as exc:
                        logger.warning("[ScriptWriter] 全剧修订集号校验失败: %s", exc)
                        _log(40, f"集号校验失败，重试 {attempt + 1}/2")
                        continue
                    parsed = candidate
                    break
                _log(40, f"修订结果解析失败，重试 {attempt + 1}/2")
            if parsed is None:
                raise ValueError("修订结果集号与原文不一致，已停止本次修订")
            new_characters = (
                parsed.get("characters")
                if isinstance(parsed.get("characters"), list)
                else (current.get("characters") or [])
            )
            new_settings = (
                parsed.get("settings")
                if isinstance(parsed.get("settings"), list)
                else (current.get("settings") or [])
            )
            preview_body = {"episodes": new_episodes}

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
                    current.get("characters") or [], new_characters, char_art.get("characters")
                )
                + script_revision.user_edited_items(
                    current.get("settings") or [], new_settings, char_art.get("settings")
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
        payload["pending_revision"] = preview
        _log(100, "修订预览已生成")
        return {"payload": payload, "requires_intervention": True, "stage_completed": False}

    async def _run_apply_revision(self, input_data: Dict[str, Any], intervention: Dict[str, Any]) -> Dict:
        """应用预览：纯落盘，不再调用 LLM。"""
        current = self._session_artifact(input_data, "script_generation")
        if not current:
            raise ValueError("剧本产物不存在，无法应用修订")
        preview = current.get("pending_revision")
        if not isinstance(preview, dict):
            raise ValueError("没有待应用的修订预览")
        if preview.get("base_version") != script_revision.content_fingerprint(current):
            raise ValueError("预览已过期或产物已变化，请重新发起修订")
        if preview.get("removed_items") and not intervention.get("confirm_removals"):
            raise ValueError("存在将被删除的角色或场景，请确认后再应用")
        if preview.get("user_edited_items") and not intervention.get("confirm_overwrites"):
            raise ValueError("存在将被覆盖的手改描述，请确认后再应用")

        updated = script_revision.apply_preview_to_artifact(
            current,
            preview,
            instruction=str(preview.get("instruction") or ""),
            scope=str(preview.get("scope") or "all"),
            episode_number=preview.get("episode_number"),
        )
        self._save_result(updated, input_data.get("session_id", ""), True)
        return {
            "payload": updated,
            "requires_intervention": False,
            "stage_completed": True,
            "revision_applied": True,
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
        if intervention and intervention.get("action") == "revise_script":
            return await self._run_revise_script(input_data, intervention)
        if intervention and intervention.get("action") == "apply_revision":
            return await self._run_apply_revision(input_data, intervention)

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
            
            last_episode_num = 0
            if input_data.get("episodes"):
                last_episode_num = input_data["episodes"][-1].get("episode_number", len(input_data["episodes"]))

            if not sequel_idea:
                _log_progress(10, "生成续写灵感...")
                idea_prompt = f"根据以下已有的剧集内容，在100字内，提供一个后续{episodes_to_add}集的简短续写灵感(主线方向): {existing_episodes_text}"
                sequel_idea = await loop.run_in_executor(None, self._cancellable_query, llm, idea_prompt, [], llm_model, True, sid, web_search)
                sequel_idea = sequel_idea.strip()

            _log_progress(30, "正在生成续写剧本文本...")
            prompt_name = "smart_continue_script"
            prompt = _get_script_prompt(prompt_name, "zh").format(
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
            sequel_script_text = await loop.run_in_executor(None, self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search)

            # 续写流程固定使用中文 prompt（与本函数上方及下方各 prompt 的硬编码 "zh" 一致）。
            # is_zh 在本函数尾部已有按标题推导的赋值，因而是本函数局部变量：必须先于
            # 下面的评估/修订 prompt 定义，否则读取时未绑定（UnboundLocalError）。
            is_zh = True
            _log_progress(50, "正在进行台词评估...")
            eval_dialogue_prompt = _get_script_prompt("eval_dialogue", "zh" if is_zh else "en").format(script_text=sequel_script_text, film_type=film_type, subject_mode=subject_mode)
            dialogue_critique = await loop.run_in_executor(None, self._cancellable_query, llm, eval_dialogue_prompt, [], llm_model, True, sid, web_search)
            
            _log_progress(55, "正在进行情节评估...")
            eval_plot_prompt = _get_script_prompt("eval_plot", "zh" if is_zh else "en").format(script_text=sequel_script_text, film_type=film_type, subject_mode=subject_mode)
            plot_critique = await loop.run_in_executor(None, self._cancellable_query, llm, eval_plot_prompt, [], llm_model, True, sid, web_search)
            
            _log_progress(58, "正在根据评估意见优化续写内容...")
            revise_prompt = _get_script_prompt("revise_script", "zh" if is_zh else "en").format(
                script_text=sequel_script_text, 
                dialogue_critique=dialogue_critique, 
                plot_critique=plot_critique,
                film_type=film_type,
                subject_mode=subject_mode
            )
            sequel_script_text = await loop.run_in_executor(None, self._cancellable_query, llm, revise_prompt, [], llm_model, True, sid, web_search)

            _log_progress(60, "提取新增人物/场景...")
            meta_prompt = _get_script_prompt("meta_extract_sequel", "zh").format(
                existing_chars=existing_chars_text,
                existing_settings=existing_settings_text,
                sequel_script=sequel_script_text
            )
            meta_raw = await loop.run_in_executor(None, self._cancellable_query, llm, meta_prompt, [], llm_model, True, sid, web_search)
            meta_res = self._extract_json_from_text(meta_raw)
            meta_data = meta_res if isinstance(meta_res, dict) else {}

            new_chars = meta_data.get("new_characters", [])
            new_settings = meta_data.get("new_settings", [])
            for c in new_chars:
                c["character_id"] = self._gen_id("char")
            for s in new_settings:
                s["setting_id"] = self._gen_id("set")

            _log_progress(80, "结构化续写集数据...")
            extract_prompt = _get_script_prompt("act_extract_sequel", "zh").format(
                sequel_script=sequel_script_text,
                start_episode_num=last_episode_num + 1,
                episodes_to_add=episodes_to_add
            )
            
            new_episodes = []
            max_retries = 3
            raw_acts = ""
            for attempt in range(max_retries):
                raw_acts = await loop.run_in_executor(None, self._cancellable_query, llm, extract_prompt, [], llm_model, True, sid, web_search)
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
            
            is_zh = any('\u4e00' <= c <= '\u9fff' for c in final_data.get("title", "Generated Script"))
            self._save_result(final_data, sid, is_zh)
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
            is_zh = any('\u4e00' <= c <= '\u9fff' for c in idea)

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
                classify_raw = await loop.run_in_executor(None, self._cancellable_query, llm, classify_prompt, [], llm_model, True, sid, web_search)
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
                    trimmed = await loop.run_in_executor(None, self._cancellable_query, llm, trim_prompt, [], llm_model, True, sid, web_search)
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
                    repaired = await loop.run_in_executor(None, self._cancellable_query, llm, repair_prompt, [], llm_model, True, sid, web_search)
                    repaired = await _trim_script_if_needed(repaired, f"{phase}集数修复后")
                return repaired

            # 1. Generate full script
            _log_progress(10, "正在生成完整剧本文本初稿...")
            prompt = _get_script_prompt("generate_script", "zh" if is_zh else "en").format(idea=idea, style=style, episodes=episodes, film_type=film_type, subject_mode=subject_mode)

            full_script_text = await loop.run_in_executor(None, self._cancellable_query, llm, prompt, [], llm_model, True, sid, web_search)
            logger.info(f"[ScriptWriter] Initial script generated ({len(full_script_text)} chars)")
            full_script_text = await _trim_script_if_needed(full_script_text, "初稿")
            full_script_text = await _repair_episode_count_if_needed(full_script_text, "初稿")
            
            _log_progress(20, "正在进行台词评估...")
            eval_dialogue_prompt = _get_script_prompt("eval_dialogue", "zh" if is_zh else "en").format(script_text=full_script_text, film_type=film_type, subject_mode=subject_mode)
            dialogue_critique = await loop.run_in_executor(None, self._cancellable_query, llm, eval_dialogue_prompt, [], llm_model, True, sid, web_search)
            
            _log_progress(30, "正在进行情节评估...")
            eval_plot_prompt = _get_script_prompt("eval_plot", "zh" if is_zh else "en").format(script_text=full_script_text, film_type=film_type, subject_mode=subject_mode)
            plot_critique = await loop.run_in_executor(None, self._cancellable_query, llm, eval_plot_prompt, [], llm_model, True, sid, web_search)
            
            _log_progress(40, "正在根据评估意见优化剧本...")
            revise_prompt = _get_script_prompt("revise_script", "zh" if is_zh else "en").format(
                script_text=full_script_text, 
                dialogue_critique=dialogue_critique, 
                plot_critique=plot_critique,
                film_type=film_type,
                subject_mode=subject_mode
            )
            full_script_text = await loop.run_in_executor(None, self._cancellable_query, llm, revise_prompt, [], llm_model, True, sid, web_search)
            logger.info(f"[ScriptWriter] Final script generated ({len(full_script_text)} chars)")
            full_script_text = await _trim_script_if_needed(full_script_text, "优化后")
            full_script_text = await _repair_episode_count_if_needed(full_script_text, "优化后")

            _log_progress(60, "最终剧本生成完成，正在提取人物/场景信息...")

            # 2. Extract meta data -> total_episodes, characters, settings
            meta_prompt = _get_script_prompt("meta_extract", "zh" if is_zh else "en").format(script_text=full_script_text, outline=full_script_text)
            meta_raw = await loop.run_in_executor(None, self._cancellable_query, llm, meta_prompt, [], llm_model, True, sid, web_search)
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
            
            raw_acts = await loop.run_in_executor(None, self._cancellable_query, llm, extract_prompt, [], llm_model, True, sid, web_search)
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
