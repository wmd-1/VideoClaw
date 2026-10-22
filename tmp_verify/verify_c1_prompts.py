from prompts.loader import load_prompt_with_fallback


def check(name, lang, **kwargs):
    template = load_prompt_with_fallback("script", name, lang)
    rendered = template.format(**kwargs)
    assert "{" in rendered and "}" in rendered  # JSON 示例已正确转义为字面量
    assert kwargs["instruction"] in rendered
    print("PASS", name, lang, len(rendered))


script_kwargs = dict(
    instruction="把第二集节奏加快",
    episodes='[{"episode_number": 1, "act_title": "t", "content": "c"}]',
    characters='[{"name": "小陈", "description": "d"}]',
    settings='[{"name": "办公室", "description": "d"}]',
    film_type="宣传",
    subject_mode="人物故事",
)
episode_kwargs = dict(
    instruction="把这一集结尾改得更明确",
    target_episode="集号：2\n标题：测试\n正文：内容",
    other_episodes_brief="第1集：概要",
    characters='[{"name": "小陈", "description": "d"}]',
    settings='[{"name": "办公室", "description": "d"}]',
    film_type="宣传",
    subject_mode="人物故事",
)

for lang in ("zh", "en"):
    check("revise_script_by_instruction", lang, **script_kwargs)
    check("revise_episode_by_instruction", lang, **episode_kwargs)

print("ALL PASS")
