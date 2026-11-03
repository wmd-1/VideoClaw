"""C3（PR-01）：模板变量完整性、中英占位符对齐与可渲染性。"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
if not PROMPTS.exists():
    # 容器内测试文件复制到 /tmp，回退到应用内提示词目录
    PROMPTS = Path("/app/prompts")
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def _vars(path: Path) -> set[str]:
    return set(_PLACEHOLDER.findall(path.read_text(encoding="utf-8")))


def test_zh_en_placeholder_sets_match():
    mismatches = []
    for zh in sorted(PROMPTS.rglob("*_zh.txt")):
        en = zh.with_name(zh.name[:-7] + "_en.txt")
        if not en.exists():
            continue
        if _vars(zh) != _vars(en):
            mismatches.append((str(zh.relative_to(PROMPTS)), sorted(_vars(zh) - _vars(en))))
    assert mismatches == [], f"中英模板占位符不一致：{mismatches}"


def test_every_template_formats_cleanly():
    for path in sorted(PROMPTS.rglob("*.txt")):
        text = path.read_text(encoding="utf-8")
        names = _vars(path)
        rendered = text.format(**{name: "X" for name in names})
        assert not _PLACEHOLDER.findall(rendered), f"残留占位符：{path}"


def test_english_first_frame_consumes_original_script():
    from prompts.loader import load_prompt

    loaded = load_prompt("reference", "first_frame", "en")
    assert "{original_text}" in loaded, "loader 的英文路径必须可达且返回新模板"
    text = (PROMPTS / "reference" / "first_frame_en.txt").read_text(encoding="utf-8")
    assert "{original_text}" in text, "英文首帧模板必须与中文一样消费完整剧本"
    rendered = text.format(
        original_text="ORIGINAL-SCRIPT",
        plot="PLOT",
        character_description="CHAR",
        setting_description="SET",
    )
    assert "ORIGINAL-SCRIPT" in rendered


def test_rewrite_prompt_orders_grounding_before_user_and_declares_conflict_rule():
    from core.agents.prompt_rewrite_agent import PromptRewriteAgent
    from prompts.loader import load_prompt

    sections = PromptRewriteAgent._render_sections(
        "AUTH\n", "CURRENT-SHOT", "【用户修改】\nUSER\n", [], "GROUNDING",
    )
    text = load_prompt("prompt_rewrite", "rewrite", "zh").format(
        input_mode="FL2VA", duration=8, segment_id="s1", **sections,
    )
    assert text.index("【当前分镜】") < text.index("【参考图/首帧实际内容")
    assert text.index("【参考图/首帧实际内容") < text.index("【用户修改】")
    assert "被固定的首帧/尾帧必须严格一致" in text
    assert "未被固定帧之间的时间轴" in text
