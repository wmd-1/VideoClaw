# -*- coding: utf-8 -*-
"""为 C1 浏览器验收注入一个可复现会话（剧本 + 阶段 2 + 分镜）。"""

from core.orchestrator import WorkflowEngine, WorkflowStage

SID = "verify_c1_ui"

EP1 = """第1集-第1场 日 内 支行办公室
人物：小陈
<action>小陈推开办公室门
<action>桌上摊着厚厚的财务报表
小陈：（叹气）这家精密零部件企业材料这么多。
小陈：报表、征信、纳税证明，后面还得算财务指标、写报告。
<action>小陈看了看手表
小陈：这效率什么时候才能提上去。
<action>小陈把材料拖入授信报告生成助手
<action>屏幕光芒一闪
小陈：（惊喜）这也太快了！
"""

EP2 = """第2集-第1场 日 内 分行审批中心
人物：小周
<action>小周坐在电脑前翻阅报告
小周：（揉眼睛）这份报告光行业分析就要从七八份研报里摘录。
<action>小周滚动鼠标翻页
小周：风险点排查得一条条对比财报数据。
<action>屏幕弹出审批报告生成完成
小周：（惊喜）以前得折腾一整天，现在几分钟就搞定了！
"""

script = {
    "project_id": f"proj_{SID}",
    "session_id": SID,
    "version": 1,
    "meta": {
        "film_type": "宣传",
        "subject_mode": "人物故事",
        "generation_model": "seed",
        "original_text": EP1 + "\n" + EP2,
    },
    "title": "对公风控研判官",
    "logline": "AI 风控助手帮助银行客户经理完成授信申报与审批审查。",
    "genre": ["金融", "科技"],
    "mood": "专业科技感",
    "characters": [
        {
            "character_id": "char_1",
            "name": "小陈",
            "description": "男，32岁，黑色短发，深色商务西装，身形偏瘦",
            "species": "人类",
        },
        {
            "character_id": "char_2",
            "name": "小周",
            "description": "女，28岁，低马尾，灰色职业套装，戴细框眼镜",
            "species": "人类",
        },
    ],
    "settings": [
        {
            "setting_id": "set_1",
            "name": "支行办公室",
            "description": "开放工位，日光灯，木质桌面，窗外有阳光",
        },
        {
            "setting_id": "set_2",
            "name": "分行审批中心",
            "description": "明亮办公区，成排工位，白色隔断，上午自然光",
        },
    ],
    "episodes": [
        {"episode_number": 1, "act_title": "授信申报", "content": EP1},
        {"episode_number": 2, "act_title": "审批审查", "content": EP2},
    ],
}

character_design = {
    "version": 1,
    "characters": [
        {
            "id": "char_1",
            "name": "小陈",
            "description": "男，32岁，黑色短发，深色商务西装，身形偏瘦",
            "species": "人类",
            "selected": "",
            "versions": ["code/result/image/verify_c1_ui/Assets/characters/char_1_v1.png"],
            "status": "done",
        },
        {
            "id": "char_2",
            "name": "小周",
            "description": "女，28岁，低马尾，灰色职业套装，戴细框眼镜",
            "species": "人类",
            "selected": "",
            "versions": [],
            "status": "pending",
        },
    ],
    "settings": [
        {
            "id": "set_1",
            "name": "支行办公室",
            "description": "开放工位，日光灯，木质桌面，窗外有阳光",
            "selected": "",
            "versions": ["code/result/image/verify_c1_ui/Assets/settings/set_1_v1.png"],
            "status": "done",
        },
        {
            "id": "set_2",
            "name": "分行审批中心",
            "description": "明亮办公区，成排工位，白色隔断，上午自然光",
            "selected": "",
            "versions": [],
            "status": "pending",
        },
    ],
}

storyboard = {
    "version": 1,
    "episodes": [
        {
            "episode_number": 1,
            "episode_title": "授信申报",
            "segments": [
                {
                    "segment_id": "seg_01_01",
                    "shots": [
                        {"shot_id": "shot_01", "plot": "小陈整理材料", "duration": 5}
                    ],
                }
            ],
        },
        {"episode_number": 2, "episode_title": "审批审查", "segments": []},
    ],
}

engine = WorkflowEngine()
state = engine.get_or_create_state(SID)
state.meta = {
    "idea": "对公风控研判官宣传片",
    "user_textbox_input": "对公风控研判官宣传片",
    "style": "realistic",
    "video_ratio": "16:9",
    "video_resolution": "720P",
    "llm_model": "deepseek-v4.1-flash",
    "vlm_model": "local-vlm",
    "image_t2i_model": "local-image-t2i",
    "image_it2i_model": "local-image-it2i",
    "video_first_frame_model": "m3",
    "video_start_end_model": "",
    "video_reference_model": "",
    "video_generation_mode": "first_frame",
    "video_model": "m3",
    "episodes": 2,
    "web_search": False,
    "enable_concurrency": True,
}
state.artifacts = {
    "script_generation": script,
    "character_design": character_design,
    "storyboard": storyboard,
}
state.status = {
    stage.value: "pending"
    for stage in WorkflowStage
    if stage not in (WorkflowStage.INIT, WorkflowStage.COMPLETED)
}
state.status["script_generation"] = "completed"
state.status["character_design"] = "waiting"
state.status["storyboard"] = "completed"
state.current_stage = WorkflowStage.SCRIPT_GENERATION
engine.save_session_to_disk(SID)
print("SEEDED", SID)
