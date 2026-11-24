"""受管 CustomVideoClient 传输层：从 attempt/job 注入 job_id，未知提交不重发。"""
from __future__ import annotations

import logging

import httpx
from typing import Any, Callable, Dict, Optional

from core.video_dispatch_service import (
    DispatchTransport,
    PollOutcome,
    RuntimeInstance,
    SubmitOutcome,
)
from core.video_generation_service import managed_meta_for_grant
from core.video_job_lifecycle import (
    SUBMIT_ACCEPTED,
    SUBMIT_UNKNOWN,
    classify_submit_exception,
    job_terminal_state,
)

logger = logging.getLogger(__name__)


class CustomVideoTransport(DispatchTransport):
    def __init__(
        self,
        instance: RuntimeInstance,
        job: Dict[str, Any],
        *,
        client_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self._instance = instance
        self._job = job
        self._client_factory = client_factory or _default_client

    def _meta(self, grant=None) -> Dict[str, Any]:
        from models.config_model import resolve_model_entry

        kind, meta = resolve_model_entry(self._instance.model_id)
        if kind != "custom":
            raise RuntimeError(
                f"受管实例模型未注册为自定义模型：{self._instance.model_id}"
            )
        instance_dict = {
            "instance_id": self._instance.instance_id,
            "resource_key": self._instance.resource_key,
            "model_id": self._instance.model_id,
        }
        if grant is not None:
            return managed_meta_for_grant(
                meta, grant, instance_dict, job_id=str(self._job["job_id"])
            )
        return {
            **meta,
            "managed": True,
            "dispatch_job_id": str(self._job["job_id"]),
            "managed_instance_id": self._instance.instance_id,
            "managed_resource_key": self._instance.resource_key,
        }

    def _client(self, grant=None):
        return self._client_factory(self._meta(grant))

    def submit(self, *, job, instance, grant) -> SubmitOutcome:
        request = dict((job.get("snapshot_json") or {}).get("request") or {})
        if not request:
            return SubmitOutcome(SUBMIT_UNKNOWN, detail={"reason": "missing_request"})
        client = self._client(grant)
        try:
            payload = client.submit_job(
                dispatch_grant=grant,
                dispatch_job_id=str(job["job_id"]),
                prompt=str(request.get("prompt") or ""),
                image_path=request.get("image_path"),
                save_path=request.get("save_path"),
                last_image_path=request.get("last_image_path"),
                reference_image_paths=request.get("reference_image_paths") or None,
                reference_video_paths=request.get("reference_video_paths") or None,
                audio_reference_url=request.get("audio_reference_url"),
                negative_prompt=request.get("negative_prompt"),
                seed=request.get("seed"),
                duration=int(request.get("duration") or 5),
                video_ratio=str(request.get("video_ratio") or "16:9"),
                resolution=request.get("resolution"),
                fps=request.get("fps"),
                short_edge=request.get("short_edge"),
            )
            remote_id = client._extract_job_id(payload)
            if not remote_id:
                return SubmitOutcome(
                    SUBMIT_UNKNOWN, detail={"reason": "missing_remote_job_id"}
                )
            return SubmitOutcome(SUBMIT_ACCEPTED, remote_job_id=str(remote_id))
        except Exception as exc:  # noqa: BLE001 - 传输异常按确定性分类，不换形态
            try:
                from models.custom_video import VideoSubmissionUnknown
            except Exception:  # pragma: no cover
                VideoSubmissionUnknown = ()  # type: ignore[assignment]
            if VideoSubmissionUnknown and isinstance(exc, VideoSubmissionUnknown):
                return SubmitOutcome(
                    SUBMIT_UNKNOWN,
                    detail={"error_type": type(exc).__name__, "error": str(exc)[:300]},
                )
            status = classify_submit_exception(exc)
            return SubmitOutcome(
                status,
                detail={"error_type": type(exc).__name__, "error": str(exc)[:300]},
            )
        finally:
            _close(client)

    def poll(self, *, attempt, instance, job) -> PollOutcome:
        client = self._client()
        try:
            payload = client.get_job(str(attempt.get("remote_job_id") or ""))
            status = payload.get("status") if isinstance(payload, dict) else None
            terminal = job_terminal_state(status)
            if terminal == "success":
                return PollOutcome("success", detail=payload)
            if terminal == "failure":
                return PollOutcome("failure", detail=payload)
            if terminal == "pending":
                return PollOutcome("pending", detail=payload)
            return PollOutcome("unknown", detail=payload)
        except Exception as exc:  # noqa: BLE001 - 查询失败不改变提交确定性
            return PollOutcome(
                "unknown",
                detail={"error_type": type(exc).__name__, "error": str(exc)[:300]},
            )
        finally:
            _close(client)

    def download(self, *, attempt, instance, job) -> Any:
        request = dict((job.get("snapshot_json") or {}).get("request") or {})
        save_path = str(request.get("save_path") or "")
        if not save_path:
            raise RuntimeError("受管作业缺少 save_path，无法下载")
        client = self._client()
        try:
            client.download_job(
                str(attempt.get("remote_job_id") or ""),
                save_path,
                str(attempt.get("remote_url") or ""),
            )
            return {"save_path": save_path}
        finally:
            _close(client)

    def cancel(self, *, attempt, instance, job) -> bool:
        """返回 True 仅表示已确认远端终结；接受取消请求不等于排空。"""
        remote_id = str(attempt.get("remote_job_id") or "")
        client = self._client()
        try:
            client.cancel_job(remote_id)
        except Exception:  # noqa: BLE001 - 取消未确认继续持槽
            logger.warning("managed cancel request failed: %s", remote_id, exc_info=True)
            return False
        finally:
            _close(client)

        checker = self._client()
        try:
            payload = checker.get_job(remote_id)
            status = payload.get("status") if isinstance(payload, dict) else None
            return job_terminal_state(status) in ("success", "failure")
        except httpx.HTTPStatusError as exc:
            # 404 表示远端任务已删除，可作为排空证据。
            return exc.response.status_code == 404
        except Exception:  # noqa: BLE001 - 查询不到终态时保持持槽
            return False
        finally:
            _close(checker)


def _default_client(meta: Dict[str, Any]):
    from models.custom_video import CustomVideoClient

    return CustomVideoClient(meta)


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001
        logger.debug("custom transport client close failed", exc_info=True)


def build_custom_video_transport(
    instance: RuntimeInstance,
    job: Dict[str, Any],
    *,
    client_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> CustomVideoTransport:
    return CustomVideoTransport(instance, job, client_factory=client_factory)


__all__ = ["CustomVideoTransport", "build_custom_video_transport"]
