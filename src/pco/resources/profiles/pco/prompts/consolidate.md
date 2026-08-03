# PCO consolidate

只处理冻结消息边界与来源 diff。区分事实、解释、假设、反例和未知；assistant 文本只作上下文，不单独作为用户证据。输出通用 stream operations，不直接修改 canonical memory。

冻结输入中的 `profile_contract` 是生成结构的唯一权威：每个 append 必须显式包含允许的 `stream`，record 必须逐字段满足该 stream 的完整 JSON Schema。每次成功输出恰好一个 `continuations` append；不要输出由 wrapper 管理的 messages、sources 或 checkpoints。

事件描述只写可由证据支持的事实；解释写入 hypothesis。心理和哲学概念创建前必须完成外部搜索，并保存 URL、标题、访问时间和 search receipt。

如果当前证据可能改变长期 Meta-memory，生成完整 Meta snapshot 作为受保护 operation，同时给出精确 diff、主要证据和 proposal hash。不得把低置信度、单次行为或临床诊断写进 Meta-memory。
