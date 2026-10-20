# tasks 分隔符共享夹具

本目录是**验收夹具，受版本控制，禁止清理**。

`tasks_separators.json` 同时被以下两侧读取：

- 后端：`tmp_verify/verify_task_separators.py`（容器内执行，读取复制到 `/tmp` 的同一文件）
- 前端：`tmp_verify/verify_task_separators.mjs`（宿主机 Node 执行，直接读本文件）

任一侧修改分隔符集合或拆分规则，都必须让另一侧用例失败，以此防止前后端口径漂移
（openspec: review-fifth-minor-followups M-1）。

注意：`.gitignore` 仅忽略 `tmp_verify/*.json` 顶层文件，本子目录会正常入库。
