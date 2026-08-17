import { strict as assert } from "node:assert"
import { appendFileSync, chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { PCOPlugin } from "../packages/pco/src/pco/resources/opencode/plugins/pco.ts"

const root = mkdtempSync(join(tmpdir(), "pco-opencode-loopback-"))
const state = join(root, "state")
mkdirSync(state, { recursive: true })
writeFileSync(join(state, "harness-binding.json"), JSON.stringify({
  id: "binding-1",
  harness: "opencode",
  native_session_id: "session-1",
}))

const callsPath = join(root, "calls.jsonl")
const fakePco = join(root, "fake-pco.ts")
writeFileSync(fakePco, `#!/usr/bin/env bun
import { appendFileSync } from "node:fs"
const args = process.argv.slice(2)
appendFileSync(${JSON.stringify(callsPath)}, JSON.stringify(args) + "\\n")
if (args.includes("status")) {
  console.log(JSON.stringify({ ok: true, checkpoint: { status: "AWAITING_META_APPROVAL" }, proposal: {
    checkpoint_id: "ckpt_1",
    proposal_hash: "sha256:proposal_1",
    approval_challenge_id: "challenge_1",
    protected_diff: [{ id: "meta_1", before: null, after: { status: "active" } }]
  }}))
} else if (args.includes("request")) {
  const n = args.filter((value) => value === "request").length
  console.log(JSON.stringify({ ok: true, approval_required: true, proposal: {
    checkpoint_id: "ckpt_" + n,
    proposal_hash: "sha256:proposal_" + n,
    approval_challenge_id: "challenge_" + n,
    protected_diff: [{ id: "meta_" + n, before: null, after: { status: "active" } }]
  }}))
} else if (args.includes("auto-probe")) {
  console.log(JSON.stringify({ ok: true, needed: true }))
} else {
  console.log(JSON.stringify({ ok: true, receipt: { context_bundle: { content_hash: "sha256:missing" } } }))
}
`)
chmodSync(fakePco, 0o755)

process.env.PCO_WORKSPACE = root
process.env.PCO_COMMAND = fakePco

const logs: unknown[] = []
let autoCommandMessageID = ""
const autoCheckpointCallID = "auto-checkpoint-call"
let skipNextAutoCheckpoint = false
let activeToolCallID = autoCheckpointCallID
let activeToolMessageID = "assistant-auto"
let activeToolParentID = ""
const plugin = await PCOPlugin({
  client: {
    app: { log: async (value: unknown) => { logs.push(value) } },
    session: {
      command: async (input: any) => {
        const messageID = input.body.messageID
        assert.match(messageID, /^msg_pco_auto_/)
        autoCommandMessageID = messageID
        activeToolParentID = messageID
        await plugin.event({ event: { type: "command.executed", properties: {
          name: "compact",
          sessionID: "session-1",
          messageID: "assistant-auto",
        } } })
        if (skipNextAutoCheckpoint) {
          skipNextAutoCheckpoint = false
          return
        }
        activeToolCallID = autoCheckpointCallID
        activeToolMessageID = "assistant-auto"
        const output = { args: {} as Record<string, unknown> }
        await plugin["tool.execute.before"]({ tool: "pco_checkpoint", sessionID: "session-1", callID: autoCheckpointCallID }, output)
        await assert.rejects(() => plugin["tool.execute.before"]({ tool: "pco_checkpoint", sessionID: "session-1", callID: "duplicate-auto-call" }, { args: {} }))
        await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-auto", metadata: () => undefined })
      },
      messages: async () => ({ data: [{
        info: { id: activeToolMessageID, parentID: activeToolParentID },
        parts: [{ type: "tool", callID: activeToolCallID }],
      }] }),
      prompt: async () => { throw new Error("pco-status must not submit a nested session prompt") },
    },
  },
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any

const context = { sessionID: "session-1", metadata: () => undefined }
const childContext = { sessionID: "session-child", metadata: () => undefined }
const emit = async (event: unknown) => await plugin.event({ event })
const beforeQuestion = async (callID: string) => {
  const output = { args: {} as Record<string, unknown> }
  await plugin["tool.execute.before"]({ tool: "question", sessionID: "session-1", callID }, output)
  assert.equal((output.args.questions as any[])[0].custom, true)
  return output
}

await assert.rejects(() => plugin.tool.pco_status.execute({}, childContext))
await assert.rejects(() => plugin.tool.pco_retry.execute({}, childContext))
await assert.rejects(() => plugin.tool.pco_abort.execute({}, childContext))

await plugin.tool.pco_checkpoint.execute({}, context)
await beforeQuestion("call-dismiss")
await emit({ type: "question.asked", properties: {
  id: "request-dismiss",
  sessionID: "session-1",
  tool: { callID: "call-dismiss" },
} })
await emit({ type: "question.dismissed", properties: {
  requestID: "request-dismiss",
  sessionID: "session-1",
} })
await plugin.tool.pco_status.execute({}, context)
await beforeQuestion("call-yes")
await emit({ type: "question.asked", properties: {
  id: "request-yes",
  sessionID: "session-1",
  tool: { callID: "call-yes" },
} })
await emit({ type: "question.replied", properties: {
  requestID: "request-yes",
  sessionID: "session-1",
  answers: [["批准此次更新"]],
} })
await plugin.tool.pco_approve.execute({}, context)

await plugin.tool.pco_checkpoint.execute({}, context)
await beforeQuestion("call-no")
await emit({ type: "question.asked", properties: {
  id: "request-no",
  sessionID: "session-1",
  tool: { callID: "call-no" },
} })
const reason = "--not a flag"
await emit({ type: "question.replied", properties: {
  requestID: "request-no",
  sessionID: "session-1",
  answers: [[reason]],
} })
await plugin.tool.pco_reject.execute({}, context)

await emit({ type: "session.idle", properties: { sessionID: "session-1" } })

const calls = (await Bun.file(callsPath).text()).trim().split("\n").map((line) => JSON.parse(line) as string[])
const decideYes = calls.find((args) => args.includes("yes"))
const decideNo = calls.find((args) => args.includes("no"))
assert.ok(decideYes?.includes("--question-request-id") && decideYes.includes("request-yes"))
assert.ok(decideNo?.includes("--question-request-id") && decideNo.includes("request-no"))
assert.ok(decideNo?.includes(`--reason=${reason}`))
const autoRequest = calls.find((args) => args.includes("request") && args.includes("--trigger") && args.includes("auto"))
assert.ok(autoRequest)

skipNextAutoCheckpoint = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
activeToolCallID = "manual-checkpoint-call"
activeToolMessageID = "assistant-manual"
activeToolParentID = "manual-command-message"
const manualOutput = { args: {} as Record<string, unknown> }
await plugin["tool.execute.before"]({ tool: "pco_checkpoint", sessionID: "session-1", callID: activeToolCallID }, manualOutput)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: activeToolMessageID, metadata: () => undefined })
const manualRequest = (await Bun.file(callsPath).text()).trim().split("\n").map((line) => JSON.parse(line) as string[])
  .find((args) => args.includes("request") && args.includes("--trigger") && args.includes("manual"))
assert.ok(manualRequest)
assert.ok(logs.length >= 2)
