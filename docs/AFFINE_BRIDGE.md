# AFFiNE projection bridge

PCO 的 AFFiNE projector 负责 canonical → projection 的调度、outbox、Git commit 幂等键和 entity mapping；bridge 只负责连接某个具体 AFFiNE 部署并完成页面 upsert。

这样划分是有意的。AFFiNE 当前的 Markdown importer 位于前端 BlockSuite 层，服务端文档内容通过 Yjs/CRDT 同步，而不是稳定的语义文档写入 API。PCO 因此不直接依赖 AFFiNE 的私有二进制协议。可核对 AFFiNE 官方仓库中的 [Markdown import implementation](https://github.com/toeverything/AFFiNE/blob/fdfb6df8260577efd03ca3679e3310702a8f69e0/blocksuite/affine/widgets/linked-doc/src/transformers/markdown.ts#L660-L691) 和官方仓库的 [API discussion](https://github.com/toeverything/AFFiNE/discussions/6052)。

## 配置

把可执行命令放入环境变量；命令由参数数组执行，不经过 shell：

```bash
export PCO_AFFINE_COMMAND='/opt/pco-affine-bridge/bin/bridge --workspace my-affine-space'
export PCO_AFFINE_TIMEOUT_SECONDS=120
```

凭据只能由 bridge 自己从环境变量或本地 secret store 读取，不能写入 stdout、canonical Git 或 mapping 文件。

## stdin 请求

bridge 每次从 stdin 读取一个 JSON object：

```json
{
  "operation": "upsert_pages",
  "memory_commit": "40-char-git-sha",
  "pages": [
    {
      "entity_id": "evt_example",
      "stream": "events",
      "title": "页面标题",
      "content": "# Markdown body\n"
    }
  ],
  "mapping": {
    "evt_existing": {
      "target_id": "affine-page-id",
      "last_commit": "previous-git-sha",
      "stream": "events"
    }
  }
}
```

bridge 必须按 `entity_id` upsert：已有 mapping 时更新同一 AFFiNE 文档；没有 mapping 时创建一篇文档，并把 PCO entity ID 保存在页面可检查的属性或正文中。`pco://<entity-id>` 链接应在目标端转换成对应页面链接。

## stdout 响应

成功时 stdout 只能输出一个 JSON object，并确认同一个 commit、返回本批次每个 entity 的非空 target ID：

```json
{
  "ok": true,
  "memory_commit": "40-char-git-sha",
  "mapping": {
    "pco_home": "affine-home-page-id",
    "evt_example": "affine-event-page-id"
  }
}
```

缺失 entity、commit 不一致、无效 JSON 或非零退出都不会推进 `.pco/state/affine/mapping.json`。PCO 保留 outbox 并返回 retryable pending。非零退出的 stderr 只写入权限为 `0600` 的本地诊断文件，receipt 不回显，避免泄漏 token。

## 幂等与恢复

- PCO 以 canonical Git commit 为 batch 幂等键。
- bridge 还必须以 entity ID 为页面幂等键；同一请求重复执行不能创建第二篇页面。
- bridge 应在所有页面成功后才输出成功响应。
- PCO 只在完整响应通过校验后原子更新 mapping。
- 删除或编辑 AFFiNE 页面不会反向修改 canonical memory；纠正必须回到 PCO 对话。

不配置 bridge 时，AFFiNE 投影会处于 pending，但 canonical commit、已批准 Meta、context publication 和 compact 不受影响。若部署暂时不需要 AFFiNE，可在初始化时选择 `--projection markdown`。
