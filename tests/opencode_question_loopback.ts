import { strict as assert } from "node:assert"
import { appendFileSync, chmodSync, mkdirSync, mkdtempSync, unlinkSync, writeFileSync } from "node:fs"
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
let autoCommandSequence = 0
let firstAutoCommandParts: any[] | undefined
let firstAutoCommandMessageID = ""
const autoCheckpointCallID = "auto-checkpoint-call"
let skipNextAutoCheckpoint = false
let holdNextAutoCommand = false
let pendingAutoArguments = ""
let activeToolCallID = autoCheckpointCallID
let activeToolMessageID = "assistant-auto"
let activeToolParentID = ""
let fullHistoryPage = false
let holdNextMessageLookup = false
let releaseMessageLookup: (() => void) | undefined
let plugin: any
const runAutoTurn = async (argumentsValue: string) => {
  const commandOutput = { parts: [{ type: "text", text: "/compact" }] }
  await plugin["command.execute.before"]({ command: "compact", sessionID: "session-1", arguments: argumentsValue }, commandOutput)
  autoCommandMessageID = `msg_19a000000000${++autoCommandSequence}_auto`
  activeToolParentID = autoCommandMessageID
  if (!firstAutoCommandParts) {
    firstAutoCommandMessageID = autoCommandMessageID
    firstAutoCommandParts = commandOutput.parts.map((part) => ({
      ...part,
      metadata: part.metadata ? { ...part.metadata } : undefined,
    }))
  }
  // OpenCode clones command parts before delivering chat.message. Exercise
  // that boundary so provenance cannot accidentally depend on object identity.
  const deliveredParts = commandOutput.parts.map((part) => ({
    ...part,
    metadata: part.metadata ? { ...part.metadata } : undefined,
  }))
  await plugin["chat.message"](
    { sessionID: "session-1", messageID: autoCommandMessageID },
    { message: { id: autoCommandMessageID }, parts: deliveredParts },
  )
  if (skipNextAutoCheckpoint) {
    skipNextAutoCheckpoint = false
    return
  }
  activeToolCallID = autoCheckpointCallID
  activeToolMessageID = "assistant-auto"
  const output = { args: {} as Record<string, unknown> }
  await plugin["tool.execute.before"]({ tool: "pco_checkpoint", sessionID: "session-1", callID: autoCheckpointCallID }, output)
  await assert.rejects(() => plugin["tool.execute.before"]({ tool: "pco_checkpoint", sessionID: "session-1", callID: "duplicate-auto-call" }, { args: {} }), "duplicate auto tool must fail")
  await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-auto", metadata: () => undefined })
}
const loopbackClient = {
    app: { log: async (value: unknown) => { logs.push(value) } },
    session: {
      command: async (input: any) => {
        assert.equal(input.body.messageID, undefined)
        if (holdNextAutoCommand) {
          holdNextAutoCommand = false
          pendingAutoArguments = input.body.arguments
          return
        }
        await runAutoTurn(input.body.arguments)
      },
      messages: async () => {
        if (holdNextMessageLookup) {
          holdNextMessageLookup = false
          await new Promise<void>((resolve) => { releaseMessageLookup = resolve })
          releaseMessageLookup = undefined
        }
        const current = {
          info: { id: activeToolMessageID, parentID: activeToolParentID },
          parts: [{ type: "tool", callID: activeToolCallID }],
        }
        if (!fullHistoryPage) return { data: [current] }
        return { data: [
          ...Array.from({ length: 9999 }, (_, index) => ({
            info: { id: `history-${index}`, parentID: "history-parent" },
            parts: [],
          })),
          current,
        ] }
      },
    },
}
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any

const context = { sessionID: "session-1", metadata: () => undefined }
const childContext = { sessionID: "session-child", metadata: () => undefined }
const emit = async (event: unknown) => await plugin.event({ event })
const runManualCheckpoint = async (
  command: "compact" | "consolidate",
  callID: string,
  commandMessageID: string,
  toolMessageID: string,
) => {
  const commandOutput = { parts: [{ type: "text", text: `/${command}` }] }
  await plugin["command.execute.before"]({ command, sessionID: "session-1", arguments: "" }, commandOutput)
  activeToolCallID = callID
  activeToolMessageID = toolMessageID
  activeToolParentID = commandMessageID
  await plugin["chat.message"](
    { sessionID: "session-1", messageID: commandMessageID },
    { message: { id: commandMessageID }, parts: commandOutput.parts },
  )
  await plugin["tool.execute.before"](
    { tool: "pco_checkpoint", sessionID: "session-1", callID },
    { args: {} },
  )
  await plugin.tool.pco_checkpoint.execute({}, {
    sessionID: "session-1",
    messageID: toolMessageID,
    metadata: () => undefined,
  })
}
const beforeQuestion = async (callID: string) => {
  const output = { args: {} as Record<string, unknown> }
  await plugin["tool.execute.before"]({ tool: "question", sessionID: "session-1", callID }, output)
  assert.equal((output.args.questions as any[])[0].custom, true)
  return output
}

await assert.rejects(() => plugin.tool.pco_status.execute({}, childContext), "child status must fail")
await assert.rejects(() => plugin.tool.pco_retry.execute({}, childContext), "child retry must fail")
await assert.rejects(() => plugin.tool.pco_abort.execute({}, childContext), "child abort must fail")
await assert.rejects(
  () => plugin.tool.pco_checkpoint.execute({}, context),
  "a direct checkpoint tool call without command or auto provenance must fail closed",
)
await assert.rejects(
  () => plugin.tool.pco_checkpoint.execute({ intent: "compact" }, context),
  "model-supplied checkpoint intent must be rejected",
)
const externalCompactionOutput: Record<string, unknown> = {}
await assert.rejects(
  () => plugin["experimental.session.compacting"]({ sessionID: "session-1", requestID: "harness-request-1" }, externalCompactionOutput),
  "external Harness compaction must be intercepted",
)
assert.equal(externalCompactionOutput.cancel, true)
assert.equal((externalCompactionOutput.pco_compaction_gate as Record<string, unknown>).decision, "intercept")
const bypassPath = join(state, "native-compact-bypass.json")
writeFileSync(bypassPath, JSON.stringify({
  token: "bypass-token-1",
  checkpointID: "ckpt-native-1",
  sessionID: "session-1",
  attemptID: "attempt-1",
  expiresAt: Date.now() + 300_000,
}))
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
const allowedCompactionOutput: Record<string, unknown> = {}
await plugin["experimental.session.compacting"]({
  sessionID: "session-1",
  metadata: { pco_native_compact: {
    token: "bypass-token-1",
    checkpoint_id: "ckpt-native-1",
    session_id: "session-1",
    attempt_id: "attempt-1",
  } },
}, allowedCompactionOutput)
assert.equal((allowedCompactionOutput.pco_compaction_gate as Record<string, unknown>).decision, "allow_once")
assert.equal(await Bun.file(bypassPath).exists(), false)
await assert.rejects(
  () => plugin["experimental.session.compacting"]({
    sessionID: "session-1",
    metadata: { pco_native_compact: {
      token: "bypass-token-1",
      checkpoint_id: "ckpt-native-1",
      session_id: "session-1",
      attempt_id: "attempt-1",
    } },
  }, {}),
  "a consumed native compact bypass token must be rejected",
)

await runManualCheckpoint("consolidate", "initial-consolidate-call", "initial-consolidate-command", "initial-consolidate-assistant")
await runManualCheckpoint("compact", "initial-manual-call", "initial-manual-command", "initial-manual-assistant")
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

await runManualCheckpoint("compact", "second-manual-call", "second-manual-command", "second-manual-assistant")
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
const consolidateRequest = calls.find((args) => args.includes("request") && args.includes("--trigger") && args.includes("manual") && args.includes("--intent") && args.includes("consolidate"))
assert.ok(consolidateRequest)
const compactRequest = calls.find((args) => args.includes("request") && args.includes("--trigger") && args.includes("manual") && args.includes("--intent") && args.includes("compact"))
assert.ok(compactRequest)
const persistedProvenance = JSON.parse(await Bun.file(join(state, "foreground-auto-provenance.json")).text()) as Record<string, unknown>
assert.equal((persistedProvenance.marker as Record<string, unknown> | null)?.nonce, undefined)
assert.ok((persistedProvenance.tombstones as Array<Record<string, unknown>>).every((entry) => entry.nonce === undefined))

// A new auto marker may already be active while an older tool call arrives.
// The old tombstone must win before the new marker can be bound.
assert.ok(firstAutoCommandMessageID)
holdNextAutoCommand = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
assert.ok(pendingAutoArguments)
await assert.rejects(() => plugin["chat.message"](
  { sessionID: "session-1", messageID: "unknown-auto-message" },
  { message: { id: "unknown-auto-message" }, parts: [{
    type: "text",
    text: "/compact",
    metadata: { pco_auto_control: true },
  }] },
), "unknown auto nonce must fail without retiring the active marker")
// Even after the newer scheduler dispatch has reached command.execute.before,
// an older replay must not steal its marker. Only the parts owned by the
// current host dispatch may bind the current message.
const currentAutoCommandOutput = { parts: [{ type: "text", text: "/compact" }] }
await plugin["command.execute.before"](
  { command: "compact", sessionID: "session-1", arguments: pendingAutoArguments },
  currentAutoCommandOutput,
)
await assert.rejects(() => plugin["chat.message"](
  { sessionID: "session-1", messageID: "replayed-during-current-dispatch" },
  { message: { id: "replayed-during-current-dispatch" }, parts: firstAutoCommandParts ?? [] },
), "stale replay must not bind a newer observed dispatch")
const currentAutoMessageID = "current-auto-command-message"
activeToolParentID = currentAutoMessageID
await plugin["chat.message"](
  { sessionID: "session-1", messageID: currentAutoMessageID },
  { message: { id: currentAutoMessageID }, parts: currentAutoCommandOutput.parts },
)
activeToolCallID = "current-auto-call"
activeToolMessageID = "assistant-current-auto"
await plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "current-auto-call" },
  { args: {} },
)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-current-auto", metadata: () => undefined })
pendingAutoArguments = ""
activeToolCallID = "old-call-during-new-auto"
activeToolMessageID = "assistant-old-call-during-new-auto"
activeToolParentID = firstAutoCommandMessageID
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "old-call-during-new-auto" },
  { args: {} },
), "old tombstone must not touch a newer auto marker")

// The second identical tool call must be rejected while the first lookup is
// still in flight; it must not race into a manual checkpoint.
holdNextMessageLookup = true
const concurrentAutoTask = emit({ type: "session.idle", properties: { sessionID: "session-1" } })
for (let attempt = 0; attempt < 200 && !releaseMessageLookup; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 5))
}
assert.ok(releaseMessageLookup)
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: autoCheckpointCallID },
  { args: {} },
), "concurrent duplicate auto tool must fail closed")
releaseMessageLookup?.()
await concurrentAutoTask

// Replaying the same stale auto control turn must not overwrite the original
// command provenance. The original tool parent must remain rejected after two
// retries, even though each replay carries a different host message ID.
assert.ok(firstAutoCommandParts)
assert.ok(firstAutoCommandMessageID)
await assert.rejects(() => plugin["chat.message"](
  { sessionID: "session-1", messageID: "stale-auto-retry-1" },
  { message: { id: "stale-auto-retry-1" }, parts: firstAutoCommandParts },
), "first stale auto replay must fail")
await assert.rejects(() => plugin["chat.message"](
  { sessionID: "session-1", messageID: "stale-auto-retry-2" },
  { message: { id: "stale-auto-retry-2" }, parts: firstAutoCommandParts },
), "second stale auto replay must fail")
activeToolCallID = "replayed-new-parent-call"
activeToolMessageID = "assistant-replayed-new-parent"
activeToolParentID = "stale-auto-retry-2"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "replayed-new-parent-call" },
  { args: {} },
), "stale auto tool with a new host parent must fail closed")
activeToolCallID = "replayed-old-auto-call"
activeToolMessageID = "assistant-replayed-old-auto"
activeToolParentID = firstAutoCommandMessageID
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "replayed-old-auto-call" },
  { args: {} },
), "replayed old auto tool must fail closed")

skipNextAutoCheckpoint = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
// Rehydrate an active auto marker after a plugin restart. An unresolvable
// delayed tool call must fail closed instead of becoming a manual checkpoint.
activeToolCallID = "restarted-stale-auto-call"
activeToolMessageID = "assistant-restarted-stale-auto"
activeToolParentID = "restarted-unknown-parent"
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "restarted-stale-auto-call" },
  { args: {} },
), "restarted stale auto tool must fail closed")
activeToolCallID = "manual-checkpoint-call"
activeToolMessageID = "assistant-manual"
activeToolParentID = "manual-command-message"
const manualCommandOutput = { parts: [{ type: "text", text: "/compact" }] }
await plugin["command.execute.before"]({ command: "compact", sessionID: "session-1", arguments: "" }, manualCommandOutput)
activeToolCallID = autoCheckpointCallID
activeToolMessageID = "assistant-late-auto"
activeToolParentID = autoCommandMessageID
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: autoCheckpointCallID },
  { args: {} },
), "late auto tool must fail")
activeToolCallID = "manual-checkpoint-call"
activeToolMessageID = "assistant-manual"
activeToolParentID = "manual-command-message"
await plugin["chat.message"](
  { sessionID: "session-1", messageID: "manual-command-message" },
  { message: { id: "manual-command-message" }, parts: manualCommandOutput.parts },
)
const manualOutput = { args: {} as Record<string, unknown> }
await plugin["tool.execute.before"]({ tool: "pco_checkpoint", sessionID: "session-1", callID: activeToolCallID }, manualOutput)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: activeToolMessageID, metadata: () => undefined })
const manualRequest = (await Bun.file(callsPath).text()).trim().split("\n").map((line) => JSON.parse(line) as string[])
  .find((args) => args.includes("request") && args.includes("--trigger") && args.includes("manual"))
assert.ok(manualRequest)

// A manual /compact can reach command.execute.before before its host message
// is delivered. If an automatic control message arrives in between, it must
// not consume the pending manual binding.
const delayedManualOutput = { parts: [{ type: "text", text: "/compact" }] }
await plugin["command.execute.before"]({ command: "compact", sessionID: "session-1", arguments: "" }, delayedManualOutput)
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
await plugin["chat.message"](
  { sessionID: "session-1", messageID: "delayed-manual-message" },
  { message: { id: "delayed-manual-message" }, parts: delayedManualOutput.parts },
)
activeToolCallID = "delayed-manual-call"
activeToolMessageID = "assistant-delayed-manual"
activeToolParentID = "delayed-manual-message"
await plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "delayed-manual-call" },
  { args: {} },
)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-delayed-manual", metadata: () => undefined })

// A manual control message can be known before a later automatic marker is
// bound. If its tool call arrives first, accepting it must retire the active
// auto marker so the delayed auto call cannot submit a duplicate request.
const knownManualOutput = { parts: [{ type: "text", text: "/compact" }] }
await plugin["command.execute.before"]({ command: "compact", sessionID: "session-1", arguments: "" }, knownManualOutput)
await plugin["chat.message"](
  { sessionID: "session-1", messageID: "known-manual-message" },
  { message: { id: "known-manual-message" }, parts: knownManualOutput.parts },
)
holdNextAutoCommand = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
assert.ok(pendingAutoArguments)
const knownAutoOutput = { parts: [{ type: "text", text: "/compact" }] }
await plugin["command.execute.before"](
  { command: "compact", sessionID: "session-1", arguments: pendingAutoArguments },
  knownAutoOutput,
)
await plugin["chat.message"](
  { sessionID: "session-1", messageID: "known-auto-message" },
  { message: { id: "known-auto-message" }, parts: knownAutoOutput.parts },
)
activeToolCallID = "known-manual-call"
activeToolMessageID = "assistant-known-manual"
activeToolParentID = "known-manual-message"
await plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "known-manual-call" },
  { args: {} },
)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-known-manual", metadata: () => undefined })
activeToolCallID = "late-known-auto-call"
activeToolMessageID = "assistant-late-known-auto"
activeToolParentID = "known-auto-message"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "late-known-auto-call" },
  { args: {} },
), "manual provenance must retire the concurrent auto marker")

holdNextAutoCommand = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
assert.ok(pendingAutoArguments)
const racingManualOutput = { parts: [{ type: "text", text: "/compact" }] }
await plugin["command.execute.before"]({ command: "compact", sessionID: "session-1", arguments: "" }, racingManualOutput)
await plugin["chat.message"](
  { sessionID: "session-1", messageID: "manual-race-message" },
  { message: { id: "manual-race-message" }, parts: racingManualOutput.parts },
)
activeToolCallID = "manual-race-call"
activeToolMessageID = "assistant-manual-race"
activeToolParentID = "manual-race-message"
await plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "manual-race-call" },
  { args: {} },
)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-manual-race", metadata: () => undefined })
activeToolCallID = "unbound-auto-call"
activeToolMessageID = "assistant-unbound-auto"
activeToolParentID = "unbound-auto-parent"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "unbound-auto-call" },
  { args: {} },
), "unbound auto tombstone must fail closed")
await assert.rejects(() => runAutoTurn(pendingAutoArguments), "stale auto command must fail")

// An unresolved old tombstone must not retire a newer marker. The new auto
// command is held so the old call is observed while its marker is pending.
holdNextAutoCommand = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
assert.ok(pendingAutoArguments)
activeToolCallID = "unresolved-old-auto-call"
activeToolMessageID = "assistant-unresolved-old-auto"
activeToolParentID = "missing-old-parent"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "unresolved-old-auto-call" },
  { args: {} },
), "unresolved old auto call must fail without retiring the newer marker")
await runAutoTurn(pendingAutoArguments)

for (const recoveryCommand of ["pco-abort", "pco-retry"]) {
  holdNextAutoCommand = true
  await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
  assert.ok(pendingAutoArguments)
  await plugin["command.execute.before"](
    { command: recoveryCommand, sessionID: "session-1", arguments: "" },
    { parts: [{ type: "text", text: `/${recoveryCommand}` }] },
  )
  activeToolCallID = `${recoveryCommand}-late-auto-call`
  activeToolMessageID = `${recoveryCommand}-late-auto-assistant`
  activeToolParentID = `${recoveryCommand}-late-auto-parent`
  await assert.rejects(() => plugin["tool.execute.before"](
    { tool: "pco_checkpoint", sessionID: "session-1", callID: activeToolCallID },
    { args: {} },
  ), `${recoveryCommand} must reject delayed auto checkpoint`)
  await assert.rejects(() => runAutoTurn(pendingAutoArguments), `${recoveryCommand} must retire auto command provenance`)
}

const evictedAutoMessageID = autoCommandMessageID
for (let attempt = 0; attempt < 65; attempt += 1) await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
activeToolCallID = "evicted-auto-call"
activeToolMessageID = "assistant-evicted-auto"
activeToolParentID = evictedAutoMessageID
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "evicted-auto-call" },
  { args: {} },
), "evicted auto tool must fail closed")

// An active marker that expires while the plugin is offline becomes a
// tombstone on rehydrate, preserving rejection of delayed auto calls.
const provenancePath = join(state, "foreground-auto-provenance.json")
writeFileSync(provenancePath, JSON.stringify({
  marker: {
    sessionID: "session-1",
    nonce: "expired-after-restart",
    commandMessageID: "expired-command-message",
    expiresAt: Date.now() - 1,
  },
  tombstones: [],
  incompleteUntil: 0,
}))
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
activeToolCallID = "expired-after-restart-call"
activeToolMessageID = "assistant-expired-after-restart"
activeToolParentID = "expired-command-message"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "expired-after-restart-call" },
  { args: {} },
), "expired marker must rehydrate as a tombstone")

// Dirty normalization must preserve the persisted fail-closed window across
// repeated plugin restarts instead of writing zero before loading it.
const incompleteUntil = Date.now() + 120_000
writeFileSync(provenancePath, JSON.stringify({
  marker: {
    sessionID: "session-1",
    commandMessageID: "windowed-expired-command",
    expiresAt: Date.now() - 1,
  },
  tombstones: [],
  incompleteUntil,
}))
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
const firstNormalized = JSON.parse(await Bun.file(provenancePath).text()) as Record<string, unknown>
assert.ok(Number(firstNormalized.incompleteUntil) >= incompleteUntil)
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
const secondNormalized = JSON.parse(await Bun.file(provenancePath).text()) as Record<string, unknown>
assert.ok(Number(secondNormalized.incompleteUntil) >= incompleteUntil)

// If the plugin restarts after tool.execute.before persisted a bound call but
// before the checkpoint tool body runs, the exact call must be resumable once.
try { unlinkSync(join(state, "foreground-auto-invalidation.json")) } catch {}
writeFileSync(provenancePath, JSON.stringify({
  marker: {
    sessionID: "session-1",
    commandMessageID: "resumable-auto-command",
    toolCallID: "resumable-auto-call",
    toolMessageID: "assistant-resumable-auto",
    expiresAt: Date.now() + 300_000,
  },
  tombstones: [],
  incompleteUntil: 0,
}))
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
activeToolCallID = "resumable-auto-call"
activeToolMessageID = "assistant-resumable-auto"
activeToolParentID = "resumable-auto-command"
await plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "resumable-auto-call" },
  { args: {} },
)
await plugin.tool.pco_checkpoint.execute({}, { sessionID: "session-1", messageID: "assistant-resumable-auto", metadata: () => undefined })

// Parsed-but-invalid state is itself provenance evidence and must block calls.
for (const [label, expiresAt] of [["missing", undefined], ["zero", 0], ["negative", -1], ["non-numeric", "later"]] as const) {
  const marker: Record<string, unknown> = {
    sessionID: "session-1",
    commandMessageID: `malformed-${label}-parent`,
  }
  if (expiresAt !== undefined) marker.expiresAt = expiresAt
  writeFileSync(provenancePath, JSON.stringify({ marker, tombstones: [] }))
  plugin = await PCOPlugin({
    client: loopbackClient,
    directory: root,
    serverUrl: new URL("http://127.0.0.1:4096"),
  } as never) as any
  activeToolCallID = `malformed-${label}-call`
  activeToolMessageID = `assistant-malformed-${label}`
  activeToolParentID = `malformed-${label}-parent`
  await assert.rejects(() => plugin["tool.execute.before"](
    { tool: "pco_checkpoint", sessionID: "session-1", callID: `malformed-${label}-call` },
    { args: {} },
  ), `active marker with ${label} expiry must fail closed`)
}

// A retirement invalidation sentinel suppresses an old active marker after a
// failed main-file write and survives the next plugin initialization.
writeFileSync(provenancePath, JSON.stringify({
  marker: {
    sessionID: "session-1",
    nonce: "retired-but-stale",
    commandMessageID: "retired-command-message",
    expiresAt: Date.now() + 300_000,
  },
  tombstones: [],
  incompleteUntil: 0,
}))
writeFileSync(join(state, "foreground-auto-invalidation.json"), JSON.stringify({
  sessionID: "session-1",
  nonce: "retired-but-stale",
  commandMessageID: "retired-command-message",
  status: "expired",
  expiresAt: Date.now() + 600_000,
}))
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
activeToolCallID = "retired-stale-call"
activeToolMessageID = "assistant-retired-stale"
activeToolParentID = "retired-command-message"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "retired-stale-call" },
  { args: {} },
), "retirement invalidation must suppress stale active marker")

// The sentinel remains authoritative even when the main mirror is missing.
writeFileSync(join(state, "foreground-auto-invalidation.json"), JSON.stringify({
  sessionID: "session-1",
  nonce: "missing-main-retired",
  commandMessageID: "missing-main-command",
  status: "expired",
  expiresAt: Date.now() + 600_000,
}))
unlinkSync(provenancePath)
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
activeToolCallID = "missing-main-stale-call"
activeToolMessageID = "assistant-missing-main-stale"
activeToolParentID = "missing-main-command"
await assert.rejects(() => plugin["tool.execute.before"](
  { tool: "pco_checkpoint", sessionID: "session-1", callID: "missing-main-stale-call" },
  { args: {} },
), "invalidation sentinel must survive missing main mirror")

// An invalidation sentinel that outlives its bounded retention window must
// not be renewed on every plugin restart.
const expiredInvalidationPath = join(state, "foreground-auto-invalidation.json")
writeFileSync(expiredInvalidationPath, JSON.stringify({
  sessionID: "session-1",
  commandMessageID: "expired-invalidation-command",
  status: "expired",
  expiresAt: Date.now() - 1,
}))
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
assert.equal(await Bun.file(expiredInvalidationPath).exists(), false)

// Consuming an auto marker must durably write its tombstone before the
// checkpoint is allowed to execute. Make the invalidation path unwritable
// and verify that the bound auto call fails closed instead of proceeding with
// an in-memory-only consumption.
const consumptionInvalidationPath = join(state, "foreground-auto-invalidation.json")
try { unlinkSync(consumptionInvalidationPath) } catch {}
try { unlinkSync(provenancePath) } catch {}
plugin = await PCOPlugin({
  client: loopbackClient,
  directory: root,
  serverUrl: new URL("http://127.0.0.1:4096"),
} as never) as any
holdNextAutoCommand = true
await emit({ type: "session.idle", properties: { sessionID: "session-1" } })
assert.ok(pendingAutoArguments)
mkdirSync(consumptionInvalidationPath)
await assert.rejects(
  () => runAutoTurn(pendingAutoArguments),
  "auto checkpoint must fail closed when consumed provenance cannot be persisted",
)
assert.ok(logs.length >= 2)
