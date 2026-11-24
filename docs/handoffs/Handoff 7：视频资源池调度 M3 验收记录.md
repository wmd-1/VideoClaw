# 视频资源池调度 M3 验收记录（2026-11-24）

## 范围

- 变更：`video-resource-pool-dispatch`
- 本文只记录 M3 后端主体与 4.9/4.11 收口证据；不包含 6.x 前端、7.x 浏览器与 F8 八用户矩阵。

## 已执行验证

- 既有 `video-claw-backend` Docker 容器内全量：`1094 passed`。
- 1.6 fixture 连续两轮：`tests/test_test_isolation.py tests/test_video_dispatch_lifespan.py` 各 `4 passed`，无调度运行时/队列/loop 残留。
- `openspec validate video-resource-pool-dispatch --type change --strict` 通过。
- `docker compose restart backend` 后 `health` 为 ok，启动日志含 runtime prepared 与 startup complete。
- 已覆盖的调度链路测试：
  - 3.1 条件唤醒：空闲不 tick、enqueue 唤醒。
  - 3.2 容量：`CapacitySnapshot` 作为 grant gate，按 `execution_id` 统计 active，跨 batch 不超 per-execution 上限。
  - 3.4/3.5：快照不扩展、成员移除、missing members、`dispatch_phase`。
  - 4.2–4.5：选池来源、auto sentinel 隔离、false/null 合并、profile duration 契约。
  - 4.9：canonical 复制、audio presign 刷新、跨 owner 拒绝、发布失败清理、`published`/`published_paths`。
  - 4.11：双次 lifespan、单 worker、启动恢复顺序。
- Fake 峰值的现有证据：`test_dispatch_uses_multiple_slots_and_respects_capacity` 断言三槽三 POST、第四项 queued；`test_video_dispatch_concurrency.py` 的 F8-A/B/C/D/E 覆盖八用户、四池、零就绪、drain 与 owner 轮转。

## 未完成/限制

- 未完成真实实例资格与真机 idle→busy→idle、sync 覆盖、取消排空验收；需要可达服务和另行授权。
- 4.9 范围限 workflow auto；sandbox/Pipeline 走 4.8 的 canonical intake 与各自发布路径。
- dispatch publisher 与 workflow 阶段 publisher 在对象层可能对同一文件重复上传；阶段版本不重复，保留为已知 P2。
- 6.1–6.6 与 7.1–7.6 已完成并取证；6.7 已按降级文本关闭：沙盒同步等待、阶段级状态，不提供 job_id 轮询；Pipeline 多 job（video_job_ids）持久化并轮询 dispatch_phase。
- 标准 Pipeline 页面受构建期 `NEXT_PUBLIC_ENABLE_STANDARD` 控制，默认关闭；只做代码/build/`?task=` 只读通道验收，未做真实提交验收。
- `video_job_ids` 当前只保存 job id 列表，不保存 segment/item 映射；前端按顺序显示 `#1/#2`，不承诺片段对应关系。
- `video_job_id` 旧字段保留最后一个 job 以兼容旧前端；新前端优先使用 `video_job_ids`。
- 变更归档本身未执行。
- 8.2 规格比对：`provider-live-reachability-check` 只读/显式探活，不修改现有 `POST /api/models/test`；本变更 delta 同时保留图像 multipart part/参数注入与视频 async 受管固定形态；两份 change strict validate 通过，关键词比对通过。
- 未提交 Git、未归档、未调用真实模型。
