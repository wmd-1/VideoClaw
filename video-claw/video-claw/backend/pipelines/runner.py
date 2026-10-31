import asyncio
import logging
import traceback

from core.media_refs import materialize_media_payload
from core.ownership import legacy_owner

from . import action_transfer, digital_human, standard
from .storage import load_task, mark_completed, mark_failed, mark_running, set_runtime_inputs

logger = logging.getLogger(__name__)

PIPELINE_REGISTRY = {
    "standard": standard.run,
    "quick_create": standard.run,
    "action_transfer": action_transfer.run,
    "digital_human": digital_human.run,
}


async def run_pipeline_task(task_id: str, pipeline: str, params: dict) -> None:
    runner = PIPELINE_REGISTRY[pipeline]
    try:
        metadata = load_task(task_id) or {}
        owner = str(metadata.get("owner") or legacy_owner())
        # 任务 input 保持 canonical；runner 只拿物化副本，输出回显的物化路径按映射还原
        canonical = metadata.get("input") or params
        runtime = materialize_media_payload(canonical, {"login": owner})
        set_runtime_inputs(task_id, canonical, runtime)
        logger.info("Pipeline task started: task_id=%s pipeline=%s", task_id, pipeline)
        mark_running(task_id)
        output, artifacts = await runner(task_id, runtime)
        await asyncio.to_thread(mark_completed, task_id, output, artifacts)
        logger.info(
            "Pipeline task completed: task_id=%s pipeline=%s artifacts=%d",
            task_id,
            pipeline,
            len(artifacts or []),
        )
    except Exception as exc:
        logger.exception("Pipeline task failed: task_id=%s pipeline=%s", task_id, pipeline)
        await asyncio.to_thread(mark_failed, task_id, f"{exc}\n{traceback.format_exc()}")
