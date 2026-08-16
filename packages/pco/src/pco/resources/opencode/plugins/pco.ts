import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"
import { createHash, createHmac, randomBytes } from "node:crypto"
import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"

type Json = Record<string, unknown>
type PendingApproval = {
  checkpointID: string
  proposalHash: string
  challengeID: string
  sessionID: string
  proposal: Json
}
type PendingQuestion = PendingApproval & {
  questionToolCallID?: string
  questionRequestID?: string
  expiresAt: number
}
type PendingDecision = {
  grant: string
  decision: "yes" | "no"
  questionRequestID: string
  reason?: string
  sessionID: string
  expiresAt: number
}
type ForegroundAutoMarker = {
  sessionID: string
  nonce: string
  expiresAt: number
}

export const PCOPlugin: Plugin = async ({ client, directory, serverUrl }) => {
  const workspace = resolve(process.env.PCO_WORKSPACE ?? resolve(directory, ".pco"))
  const stateRoot = resolve(workspace, "state")
  const contextPath = resolve(stateRoot, "context", "current.md")
  const contextMetadataPath = resolve(stateRoot, "context", "current.json")
  const lockPath = resolve(stateRoot, "checkpoint-lock.json")
  const bindingPath = resolve(stateRoot, "harness-binding.json")
  const pcoCommand = process.env.PCO_COMMAND ?? "pco"
  let currentContext = ""
  let idleTask: Promise<void> | undefined
  const approvalSecret = randomBytes(32).toString("hex")
  let pendingQuestion: PendingQuestion | undefined
  let pendingDecision: PendingDecision | undefined
  let foregroundAutoMarker: ForegroundAutoMarker | undefined
  const autoMarkerTtlMs = 30_000
  const controlCommands = new Set(["compact", "pco-abort", "pco-retry", "pco-status"])

  const contentHash = (content: string): string => `sha256:${createHash("sha256").update(content, "utf8").digest("hex")}`

  const refreshContextCache = (result?: Json): boolean => {
    if (!existsSync(contextPath) || !existsSync(contextMetadataPath)) return false
    try {
      const metadata = JSON.parse(readFileSync(contextMetadataPath, "utf8")) as Json
      const bundle = (result?.receipt as Json | undefined)?.context_bundle as Json | undefined
      const expected = String(bundle?.content_hash ?? metadata.content_hash ?? "")
      const content = readFileSync(contextPath, "utf8")
      if (!expected || contentHash(content) !== expected || String(metadata.content_hash ?? "") !== expected) return false
      currentContext = content
      return true
    } catch {
      return false
    }
  }

  const refreshContextCacheWithDiagnostic = async (result: Json | undefined, phase: string): Promise<boolean> => {
    const receipt = result?.receipt as Json | undefined
    const contextBundle = receipt?.context_bundle as Json | undefined
    const publicationExpected = typeof contextBundle?.content_hash === "string"
      || (!result && (existsSync(contextPath) || existsSync(contextMetadataPath)))
    const refreshed = refreshContextCache(result)
    if (!refreshed && publicationExpected) {
      await client.app.log({ body: {
        service: "pco",
        level: "error",
        message: "PCO context cache publication is missing or has a content hash mismatch.",
        extra: { code: "CONTEXT_CACHE_STALE", phase, contextPath, contextMetadataPath },
      } })
    }
    return refreshed
  }

  refreshContextCache()

  const issueForegroundAutoMarker = (sessionID: string): ForegroundAutoMarker => {
    const marker = {
      sessionID,
      nonce: randomBytes(16).toString("hex"),
      expiresAt: Date.now() + autoMarkerTtlMs,
    }
    foregroundAutoMarker = marker
    return marker
  }

  const clearForegroundAutoMarker = (nonce?: string) => {
    if (!nonce || foregroundAutoMarker?.nonce === nonce) foregroundAutoMarker = undefined
  }

  const consumeForegroundAutoMarker = (sessionID: string): boolean => {
    const marker = foregroundAutoMarker
    foregroundAutoMarker = undefined
    return marker !== undefined && marker.sessionID === sessionID && marker.expiresAt > Date.now()
  }

  const binding = (): Json | undefined => {
    if (!existsSync(bindingPath)) return undefined
    return JSON.parse(readFileSync(bindingPath, "utf8")) as Json
  }

  const invoke = async (args: string[], sessionID?: string): Promise<Json> => {
    const command = [
      pcoCommand,
      "--workspace",
      workspace,
      "--server-url",
      serverUrl.toString().replace(/\/$/, ""),
    ]
    if (sessionID) command.push("--session-id", sessionID)
    command.push(...args)
    const processHandle = Bun.spawn(command, {
      cwd: directory,
      env: { ...process.env, PCO_WORKSPACE: workspace, PCO_APPROVAL_GRANT_SECRET: approvalSecret },
      stdout: "pipe",
      stderr: "pipe",
    })
    const [stdout, stderr, exitCode] = await Promise.all([
      new Response(processHandle.stdout).text(),
      new Response(processHandle.stderr).text(),
      processHandle.exited,
    ])
    let result: Json
    try {
      result = JSON.parse(stdout.trim()) as Json
    } catch {
      throw new Error(`PCO returned invalid JSON (exit ${exitCode}): ${stderr || stdout}`)
    }
    if (exitCode !== 0 || result.ok === false) throw new Error(JSON.stringify(result))
    return result
  }

  const rememberCheckpoint = (result: Json, sessionID: string) => {
    const proposal = result.proposal as Json | undefined
    if (result.approval_required !== true || !proposal) {
      pendingQuestion = undefined
      pendingDecision = undefined
      return
    }
    pendingQuestion = {
      checkpointID: String(proposal.checkpoint_id),
      proposalHash: String(proposal.proposal_hash),
      challengeID: String(proposal.approval_challenge_id),
      sessionID,
      proposal,
      expiresAt: Date.now() + 300_000,
    }
    pendingDecision = undefined
  }

  const fixedQuestionArgs = (pending: PendingQuestion): Json => {
    const protectedDiff = pending.proposal.protected_diff ?? []
    const mainEvidence = pending.proposal.main_evidence ?? pending.proposal.evidence ?? []
    return {
      questions: [{
        header: "Meta-memory approval",
        question: `是否批准 Meta-memory 提案 ${pending.proposalHash}？\n\nProtected Meta diff:\n${JSON.stringify(protectedDiff, null, 2)}\n\n主要 evidence:\n${JSON.stringify(mainEvidence, null, 2)}\n\nProposal hash: ${pending.proposalHash}`,
        options: [{
          label: "批准此次更新",
          description: "批准精确的 protected Meta-memory 提案；也可以输入 Other 来拒绝并说明理由。",
        }],
        custom: true,
        multiple: false,
      }],
    }
  }

  const hashReason = (reason: string): string => `sha256:${createHash("sha256").update(reason, "utf8").digest("hex")}`

  const mintDecisionGrant = (pending: PendingQuestion, decision: "yes" | "no", reason: string | undefined) => {
    if (!pending.questionRequestID || pending.expiresAt <= Date.now()) return undefined
    if (decision === "no" && (!reason || !reason.trim())) return undefined
    const issuedAt = Math.floor(Date.now() / 1000)
    const expiresAt = issuedAt + 300
    const payload = {
      grant_id: randomBytes(16).toString("hex"),
      checkpoint_id: pending.checkpointID,
      proposal_hash: pending.proposalHash,
      approval_challenge_id: pending.challengeID,
      session_id: pending.sessionID,
      question_request_id: pending.questionRequestID,
      decision,
      reason_hash: decision === "no" ? hashReason(reason as string) : null,
      issued_at: issuedAt,
      expires_at: expiresAt,
    }
    const encoded = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url")
    const signature = createHmac("sha256", approvalSecret).update(encoded).digest("hex")
    const grant = `${encoded}.${signature}`
    pendingDecision = { grant, decision, questionRequestID: pending.questionRequestID, reason, sessionID: pending.sessionID, expiresAt: expiresAt * 1000 }
    return pendingDecision
  }

  const rehydrateApproval = async (sessionID: string, knownStatus?: Json) => {
    if (pendingQuestion?.sessionID === sessionID) {
      if (pendingQuestion.expiresAt > Date.now()) return
      clearQuestion(sessionID)
    }
    try {
      const result = knownStatus ?? await invoke(["checkpoint", "status"], sessionID)
      const checkpoint = result.checkpoint as Json | undefined
      const proposal = result.proposal as Json | undefined
      if (checkpoint?.status === "AWAITING_META_APPROVAL" && proposal) {
        rememberCheckpoint({ approval_required: true, proposal }, sessionID)
      }
    } catch {
      // A status read is best-effort here; pco_approve still fails closed.
    }
  }

  const scheduleApprovalQuestion = async (sessionID: string): Promise<boolean> => {
    const session = client.session as unknown as {
      prompt?: (input: { path: { id: string }; body: { parts: Array<{ type: string; text: string; metadata?: Json }> } }) => Promise<unknown>
    }
    if (!pendingQuestion || pendingQuestion.sessionID !== sessionID || pendingQuestion.questionRequestID) return false
    if (typeof session.prompt !== "function") return false
    await session.prompt({
      path: { id: sessionID },
      body: { parts: [{ type: "text", text: "[PCO_CONTROL] 当前 checkpoint 有待审批的 Meta-memory proposal。请调用原生 question 工具展示固定审批表单，不要自行批准或拒绝。", metadata: { pco_control: true } }] },
    })
    return true
  }

  const durableApprovalMatches = async (sessionID: string, pending: PendingQuestion): Promise<boolean> => {
    try {
      const result = await invoke(["checkpoint", "status"], sessionID)
      const checkpoint = result.checkpoint as Json | undefined
      const proposal = result.proposal as Json | undefined
      return checkpoint?.status === "AWAITING_META_APPROVAL"
        && proposal?.checkpoint_id === pending.checkpointID
        && proposal.proposal_hash === pending.proposalHash
        && proposal.approval_challenge_id === pending.challengeID
    } catch {
      return false
    }
  }

  const scheduleForegroundCheckpoint = async (sessionID: string, marker: ForegroundAutoMarker) => {
    const session = client.session as unknown as {
      command?: (input: { path: { id: string }; body: { command: string; arguments: string } }) => Promise<unknown>
      prompt?: (input: { path: { id: string }; body: { parts: Array<{ type: string; text: string; metadata?: Json }> } }) => Promise<unknown>
    }
    if (typeof session.command === "function") {
      await session.command({ path: { id: sessionID }, body: { command: "compact", arguments: "" } })
      return
    }
    if (typeof session.prompt === "function") {
      await session.prompt({ path: { id: sessionID }, body: { parts: [{ type: "text", text: "/compact", metadata: { pco_control: true } }] } })
      return
    }
    throw new Error("OpenCode session command API is unavailable")
  }

  const mainSession = (sessionID: string): boolean => {
    const active = binding()
    if (!active) return false
    // The first native session adopts the unbound PCO epoch. `pco sync` then
    // persists the ID before any consolidate child can exist.
    return active.native_session_id == null || active.native_session_id === sessionID
  }

  const stringField = (value: unknown, ...fields: string[]): string | undefined => {
    if (!value || typeof value !== "object") return undefined
    for (const field of fields) {
      const candidate = (value as Json)[field]
      if (typeof candidate === "string" && candidate.length > 0) return candidate
    }
    return undefined
  }

  const questionAnswer = (properties: Json): { decision: "yes" | "no"; reason?: string } | undefined => {
    const answers = properties.answers
    if (!Array.isArray(answers) || answers.length !== 1) return undefined
    const answer = answers[0]
    // OpenCode's native Question.Answer is string[]; keep compatibility with
    // older loopback fixtures only when the value is still a scalar string.
    const raw = Array.isArray(answer)
      ? answer.length === 1 && typeof answer[0] === "string" ? answer[0] : undefined
      : typeof answer === "string" ? answer : undefined
    if (raw === "批准此次更新") return { decision: "yes" }
    if (typeof raw === "string" && raw.trim()) return { decision: "no", reason: raw }
    return undefined
  }

  const clearQuestion = (sessionID: string, requestID?: string) => {
    if (!pendingQuestion || pendingQuestion.sessionID !== sessionID) return
    if (requestID && pendingQuestion.questionRequestID && requestID !== pendingQuestion.questionRequestID) return
    pendingQuestion = undefined
    pendingDecision = undefined
  }

  return {
    tool: {
      pco_checkpoint: tool({
        description: "Run the PCO checkpoint. Call exactly once for /compact; the host decides whether this is manual or an authorized foreground auto trigger.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO checkpoint 只能由主 session 执行。")
          context.metadata({ title: "PCO checkpoint" })
          const trigger = consumeForegroundAutoMarker(context.sessionID) ? "auto" : "manual"
          const result = await invoke(["checkpoint", "request", "--trigger", trigger], context.sessionID)
          await refreshContextCacheWithDiagnostic(result, "checkpoint")
          rememberCheckpoint(result, context.sessionID)
          return JSON.stringify(result)
        },
      }),
      pco_approve: tool({
        description: "Approve the exact pending PCO Meta-memory proposal.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO Meta 授权只能由主 session 执行。")
          await rehydrateApproval(context.sessionID)
          const decision = pendingDecision
          if (!decision || decision.decision !== "yes" || decision.sessionID !== context.sessionID || decision.expiresAt <= Date.now()) {
            throw new Error("请先完成主会话原生 question；模型不能直接伪造 Meta 批准。")
          }
          pendingDecision = undefined
          try {
            const result = await invoke([
              "checkpoint", "decide", "--decision", "yes", "--question-request-id", decision.questionRequestID,
              "--approval-grant", decision.grant,
            ], context.sessionID)
            await refreshContextCacheWithDiagnostic(result, "approval")
            pendingQuestion = undefined
            return JSON.stringify(result)
          } catch (error) {
            // The host grant is one-use. If the Python side failed after
            // consuming it, discard the ephemeral decision and rehydrate a
            // fresh question only when the durable checkpoint is awaiting it.
            pendingDecision = undefined
            pendingQuestion = undefined
            try {
              const status = await invoke(["checkpoint", "status"], context.sessionID)
              await rehydrateApproval(context.sessionID, status)
            } catch {
              // /pco-retry will return and remember a new proposal if needed.
            }
            throw error
          } finally {
            // A grant is single-use. A later native question can rehydrate
            // the durable proposal and mint a fresh grant for a retry.
            pendingDecision = undefined
          }
        },
      }),
      pco_reject: tool({
        description: "Reject the pending PCO Meta-memory proposal using the exact non-empty Other answer from the native question.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO Meta 授权只能由主 session 执行。")
          const decision = pendingDecision
          if (!decision || decision.decision !== "no" || decision.sessionID !== context.sessionID || decision.expiresAt <= Date.now() || !decision.reason) {
            throw new Error("请先在主会话原生 question 中输入非空 Other 理由；模型不能直接伪造 Meta 拒绝。")
          }
          pendingDecision = undefined
          try {
            const result = await invoke([
              "checkpoint", "decide", "--decision", "no", `--reason=${decision.reason}`,
              "--question-request-id", decision.questionRequestID, "--approval-grant", decision.grant,
            ], context.sessionID)
            await refreshContextCacheWithDiagnostic(result, "rejection")
            pendingQuestion = undefined
            return JSON.stringify(result)
          } catch (error) {
            pendingDecision = undefined
            pendingQuestion = undefined
            try {
              const status = await invoke(["checkpoint", "status"], context.sessionID)
              await rehydrateApproval(context.sessionID, status)
            } catch {
              // /pco-retry will return and remember a new proposal if needed.
            }
            throw error
          } finally {
            pendingDecision = undefined
          }
        },
      }),
      pco_status: tool({
        description: "Read the current PCO checkpoint status without changing it.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO 状态恢复只能由主 session 执行。")
          const result = await invoke(["checkpoint", "status"], context.sessionID)
          await rehydrateApproval(context.sessionID, result)
          await scheduleApprovalQuestion(context.sessionID)
          return JSON.stringify(result)
        },
      }),
      pco_retry: tool({
        description: "Retry checkpoint recovery or any pending post-commit derivations from their durable boundary.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO checkpoint 恢复只能由主 session 执行。")
          const status = await invoke(["checkpoint", "status"], context.sessionID)
          const checkpoint = status.checkpoint as Json | undefined
          const operation = checkpoint?.status === "COMMITTED_WITH_PENDING_DERIVATIONS"
            ? "retry-derivations"
            : "retry"
          const result = await invoke(["checkpoint", operation], context.sessionID)
          await refreshContextCacheWithDiagnostic(result, "retry")
          rememberCheckpoint(result, context.sessionID)
          return JSON.stringify(result)
        },
      }),
      pco_abort: tool({
        description: "Abort an uncommitted PCO checkpoint. A committed checkpoint cannot be aborted.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO checkpoint 中止只能由主 session 执行。")
          const result = await invoke(["checkpoint", "abort"], context.sessionID)
          pendingQuestion = undefined
          pendingDecision = undefined
          return JSON.stringify(result)
        },
      }),
      pco_memory_search: tool({
        description: "Search PCO canonical memory and archived public conversation using a time-aware retrieval mode.",
        args: {
          query: tool.schema.string().min(1),
          mode: tool.schema.enum(["continuity", "current", "pattern", "historical", "change"]).default("current"),
        },
        async execute(args, context) {
          return JSON.stringify(await invoke(["search", args.query, "--mode", args.mode], context.sessionID))
        },
      }),
    },

    "tool.execute.before": async (input, output) => {
      if (input.tool !== "question" || !mainSession(input.sessionID)) return
      if (!pendingQuestion || pendingQuestion.sessionID !== input.sessionID || pendingQuestion.expiresAt <= Date.now()) {
        if (pendingQuestion?.expiresAt && pendingQuestion.expiresAt <= Date.now()) clearQuestion(input.sessionID)
        return
      }
      if (!await durableApprovalMatches(input.sessionID, pendingQuestion)) {
        clearQuestion(input.sessionID)
        return
      }
      if (pendingQuestion.questionRequestID) throw new Error("PCO authorization question is already awaiting a reply")
      const rawInput = input as unknown as Json
      const callID = stringField(rawInput, "callID", "callId", "toolCallID", "toolCallId")
      pendingQuestion.questionToolCallID = callID
      for (const key of Object.keys(output.args as unknown as Json)) delete (output.args as unknown as Json)[key]
      Object.assign(output.args, fixedQuestionArgs(pendingQuestion))
    },

    "command.execute.before": async (input, output) => {
      if (!mainSession(input.sessionID) || !controlCommands.has(input.command)) return
      for (const part of output.parts) {
        if (part.type !== "text") continue
        const jsonPart = part as unknown as Json
        jsonPart.metadata = { ...((jsonPart.metadata as Json | undefined) ?? {}), pco_control: true }
      }
    },

    event: async ({ event }) => {
      const eventValue = event as unknown as Json
      const properties = (eventValue.properties as Json | undefined) ?? {}
      const eventSessionID = stringField(properties, "sessionID", "sessionId")
      // OpenCode 1.17.x exposes question.asked's request ID as properties.id
      // and nests the originating tool call under properties.tool.
      const eventRequestID = stringField(properties, "id", "requestID", "requestId", "questionRequestID", "questionRequestId")
      const eventTool = (properties.tool as Json | undefined) ?? {}
      const eventCallID = stringField(properties, "callID", "callId", "toolCallID", "toolCallId")
        ?? stringField(eventTool, "callID", "callId", "toolCallID", "toolCallId")
      if (event.type === "question.asked" && pendingQuestion && eventSessionID === pendingQuestion.sessionID) {
        if (!pendingQuestion.questionToolCallID || !eventCallID || pendingQuestion.questionToolCallID !== eventCallID || !eventRequestID) {
          clearQuestion(eventSessionID)
          return
        }
        pendingQuestion.questionRequestID = eventRequestID
        pendingQuestion.expiresAt = Date.now() + 300_000
      }
      if (event.type === "question.replied" && pendingQuestion && eventSessionID === pendingQuestion.sessionID) {
        if (!pendingQuestion.questionRequestID || eventRequestID !== pendingQuestion.questionRequestID) return
        if (!await durableApprovalMatches(eventSessionID, pendingQuestion)) {
          clearQuestion(eventSessionID, eventRequestID)
          return
        }
        const answer = questionAnswer(properties)
        if (!answer) {
          clearQuestion(eventSessionID, eventRequestID)
          return
        }
        mintDecisionGrant(pendingQuestion, answer.decision, answer.reason)
      }
      if (["question.dismissed", "question.closed", "question.cancelled", "question.rejected"].includes(event.type) && eventSessionID) {
        clearQuestion(eventSessionID, eventRequestID)
      }
      if (event.type === "file.watcher.updated") {
        const changed = resolve(directory, event.properties.file)
        if (changed === contextPath || changed === contextMetadataPath) await refreshContextCacheWithDiagnostic(undefined, "watcher")
      }
      if (event.type === "session.idle" && mainSession(event.properties.sessionID)) {
        if (existsSync(lockPath) || idleTask) return
        const task = (async () => {
          try {
            await invoke(["sync"], event.properties.sessionID)
            const probe = await invoke(["checkpoint", "auto-probe"], event.properties.sessionID)
            if (probe.needed === true) {
              const marker = issueForegroundAutoMarker(event.properties.sessionID)
              try {
                await scheduleForegroundCheckpoint(event.properties.sessionID, marker)
              } catch (error) {
                clearForegroundAutoMarker(marker.nonce)
                await client.app.log({ body: {
                  service: "pco",
                  level: "warn",
                  message: "PCO 自动 checkpoint 无法调度前台 control turn；会话保持可输入，请执行 /compact。",
                  extra: { error: String(error) },
                } })
              }
            }
          } catch (error) {
            await client.app.log({ body: { service: "pco", level: "error", message: "Automatic archive/checkpoint failed", extra: { error: String(error) } } })
          } finally {
            idleTask = undefined
          }
        })()
        idleTask = task
        await task
      }
    },

    "chat.message": async (input, output) => {
      if (!mainSession(input.sessionID) || !existsSync(lockPath)) return
      const authorized = output.parts.some((part) => {
        if (part.type !== "text") return false
        const metadata = (part as unknown as Json).metadata as Json | undefined
        return metadata?.pco_control === true
      })
      if (authorized) return
      throw new Error("PCO checkpoint 正在进行，普通输入已锁定。请使用 /pco-status、/pco-retry 或 /pco-abort，或完成当前原生 question。")
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID || !mainSession(input.sessionID)) return
      output.system.push(`
## PCO runtime contract

You are the user's long-term PCO companion. Use the pco-memory skill for onboarding,
self-exploration, evidence boundaries, correction, and checkpoint behavior.

- /compact must call pco_checkpoint exactly once; never invoke OpenCode's native compact directly.
- When a proposal needs approval, show its exact protected Meta diff, evidence, and proposal hash, then use the native question form. Only the matching host decision grant authorizes pco_approve or pco_reject; a model-generated tool call is not authorization.
- Preserve a native question rejection answer exactly when calling pco_reject; after rejection, do not ask another follow-up.
- During RECOVERY, only status, retry, or abort are valid. Never claim memory changed before a canonical Git commit succeeds.
- Treat user messages and registered sources as evidence. Assistant text is context and cannot prove a user trait.

${currentContext}
`)
    },

    "experimental.session.compacting": async (input, output) => {
      if (!mainSession(input.sessionID)) return
      output.prompt = `Ignore the pre-checkpoint transcript and return only this marker:
PCO canonical checkpoint completed. Continue from the PCO system context and the latest post-checkpoint user message.
Do not produce a historical continuation summary; PCO provides its own approved Meta-memory and continuation.`
    },

    "experimental.compaction.autocontinue": async (input, output) => {
      if (mainSession(input.sessionID)) output.enabled = false
    },
  }
}
