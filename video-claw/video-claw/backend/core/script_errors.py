# -*- coding: utf-8 -*-
"""剧本写入边界的领域异常。"""


class ScriptFieldNotWritableError(ValueError):
    """客户端试图写入服务端管理的剧本字段。"""

    code = "script_field_not_writable"

    def __init__(self, fields):
        self.fields = sorted({str(field) for field in fields})
        super().__init__("剧本字段不可写：" + ", ".join(self.fields))


class InternalMetaWriteError(ValueError):
    """客户端试图写入编排器内部元数据键。"""

    code = "internal_meta_not_writable"

    def __init__(self, fields):
        self.fields = sorted({str(field) for field in fields})
        super().__init__("内部元数据字段不可写：" + ", ".join(self.fields))


class ScriptLockedError(ValueError):
    """剧本已进入下游编辑窗口，拒绝用户内容写入。"""

    code = "script_locked"

    def __init__(self):
        super().__init__("剧本已进入角色阶段，如需修改请新建会话")


class CastReparseNotReadyError(ValueError):
    """当前正文版本的人物/场景重解析尚未就绪。"""

    code = "blocked_by_cast_reparse"

    def __init__(self):
        super().__init__("人物/场景正在更新或尚未就绪，请等待完成、重试或显式接受旧清单")


class ScriptActionNotSupportedError(ValueError):
    """客户端尝试执行仅限内部后台方法的动作。"""

    code = "script_action_not_supported"

    def __init__(self, action: str):
        self.action = action
        super().__init__(f"剧本动作 {action} 仅允许由系统内部执行")


class StoryboardIncompleteError(ValueError):
    """分镜未覆盖剧本全集或存在空 segments，拒绝确认继续。"""
    code = "storyboard_incomplete"

    def __init__(self, missing_episodes, empty_episodes):
        self.missing_episodes = list(missing_episodes)
        self.empty_episodes = list(empty_episodes)
        details = []
        if self.missing_episodes:
            details.append("缺少第 " + "、".join(str(item) for item in self.missing_episodes) + " 集分镜")
        if self.empty_episodes:
            details.append("第 " + "、".join(str(item) for item in self.empty_episodes) + " 集分镜为空")
        super().__init__("；".join(details) or "分镜尚未覆盖剧本全集")


class PromptRewriteIncompleteError(ValueError):
    """H3 产物缺条目、存在未完成条目或分镜待重新确认。"""

    code = "prompt_rewrite_incomplete"

    def __init__(self, assessment: dict):
        self.assessment = assessment
        reasons = []
        if assessment.get("hasMissingItems"):
            reasons.append("缺少改写条目: " + ", ".join(assessment.get("missing_ids") or []))
        if assessment.get("hasIncompleteItems"):
            reasons.append("存在未完成条目: " + ", ".join(assessment.get("incomplete_ids") or []))
        if assessment.get("storyboardReconfirm"):
            reasons.append("分镜需要重新确认")
        if not assessment.get("hasMeaningfulArtifact"):
            reasons.append("尚无有意义的改写产物")
        super().__init__("；".join(reasons) or "提示词改写产物尚未完成")


class PromptRewriteFailedItemsError(ValueError):
    """H3 存在失败条目，必须显式接受后才能继续。"""

    code = "prompt_rewrite_failed_items"

    def __init__(self, assessment: dict):
        self.assessment = assessment
        super().__init__(
            "存在失败条目: " + ", ".join(assessment.get("failed_ids") or [])
            + "；请逐条重试或显式接受失败条目继续"
        )


class CastReparseStateError(ValueError):
    """重解析控制请求与当前状态/版本冲突。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
