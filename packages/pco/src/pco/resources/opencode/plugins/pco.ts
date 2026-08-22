import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs"
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
  expiresAt?: number
  commandMessageID?: string
  bindingToolCallID?: string
  toolCallID?: string
  toolMessageID?: string
  // Internal-only marker used to distinguish a bound call restored after a
  // plugin restart from a duplicate call in the same process. It is omitted
  // from the durable mirror below.
  restoredBoundCallID?: string
}
type ForegroundAutoTombstone = ForegroundAutoMarker & {
  status: "expired" | "consumed"
}
type ForegroundAutoDispatch = {
  sessionID: string
  nonce: string
  // This process-local token is copied into command-part metadata by the
  // host hook. OpenCode clones parts between command execution and
  // chat.message, so object identity cannot be used as the binding.
  partToken: string
  commandObserved?: boolean
}

export const PCOPlugin: Plugin = async ({ client, directory, serverUrl }) => {
  const workspace = resolve(process.env.PCO_WORKSPACE ?? resolve(directory, ".pco"))
  const stateRoot = resolve(workspace, "state")
  const contextPath = resolve(stateRoot, "context", "current.md")
  const contextMetadataPath = resolve(stateRoot, "context", "current.json")
  const lockPath = resolve(stateRoot, "checkpoint-lock.json")
  const bindingPath = resolve(stateRoot, "harness-binding.json")
  const foregroundAutoProvenancePath = resolve(stateRoot, "foreground-auto-provenance.json")
  const foregroundAutoInvalidationPath = resolve(stateRoot, "foreground-auto-invalidation.json")
  const pcoCommand = process.env.PCO_COMMAND ?? "pco"
  let currentContext = ""
  let idleTask: Promise<void> | undefined
  const approvalSecret = randomBytes(32).toString("hex")
  let pendingQuestion: PendingQuestion | undefined
  let pendingDecision: PendingDecision | undefined
  let foregroundAutoMarker: ForegroundAutoMarker | undefined
  let foregroundAutoMarkerTimer: ReturnType<typeof setTimeout> | undefined
  let foregroundAutoTombstones: ForegroundAutoTombstone[] = []
  let foregroundAutoProvenanceIncompleteUntil = 0
  let foregroundAutoProvenanceUnavailable = false
  let foregroundAutoDispatch: ForegroundAutoDispatch | undefined
  let manualCommandPendingSessionID: string | undefined
  let manualControlMessageID: string | undefined
  let manualCheckpointSessionID: string | undefined
  let manualCheckpointCallID: string | undefined
  let manualCheckpointMessageID: string | undefined
  const foregroundAutoMarkerTtlMs = 5 * 60_000
  const foregroundAutoTombstoneTtlMs = 10 * 60_000
  const foregroundAutoHistoryLimit = 10_000
  const foregroundAutoArgumentPrefix = "--pco-auto-nonce="
  const foregroundAutoTombstoneLimit = 64
  const controlCommands = new Set(["compact", "pco-abort", "pco-retry", "pco-status"])
  const recoveryCommands = new Set(["pco-abort", "pco-retry"])

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

  const markForegroundAutoProvenanceUnavailable = () => {
    foregroundAutoProvenanceUnavailable = true
    foregroundAutoProvenanceIncompleteUntil = Math.max(
      foregroundAutoProvenanceIncompleteUntil,
      Date.now() + foregroundAutoTombstoneTtlMs,
    )
  }

  const persistForegroundAutoProvenance = (): boolean => {
    try {
      writeFileSync(foregroundAutoProvenancePath, JSON.stringify({
        marker: foregroundAutoMarker
          ? {
              ...foregroundAutoMarker,
              nonce: undefined,
              bindingToolCallID: undefined,
              restoredBoundCallID: undefined,
            }
          : null,
        tombstones: foregroundAutoTombstones.map((entry) => ({
          ...entry,
          nonce: undefined,
          bindingToolCallID: undefined,
          restoredBoundCallID: undefined,
        })),
        incompleteUntil: foregroundAutoProvenanceIncompleteUntil,
      }), "utf8")
      foregroundAutoProvenanceUnavailable = false
      return true
    } catch {
      // A marker that cannot be durably mirrored must not authorize a later
      // call as manual. Keep the current process in a fail-closed state.
      markForegroundAutoProvenanceUnavailable()
      return false
    }
  }

  const writeForegroundAutoInvalidation = (marker: ForegroundAutoMarker, status: "expired" | "consumed"): boolean => {
    try {
      writeFileSync(foregroundAutoInvalidationPath, JSON.stringify({
        sessionID: marker.sessionID,
        commandMessageID: marker.commandMessageID,
        toolCallID: marker.toolCallID,
        toolMessageID: marker.toolMessageID,
        status,
        expiresAt: Date.now() + foregroundAutoTombstoneTtlMs,
      }), "utf8")
      return true
    } catch {
      markForegroundAutoProvenanceUnavailable()
      return false
    }
  }

  const clearForegroundAutoInvalidation = () => {
    try {
      if (existsSync(foregroundAutoInvalidationPath)) unlinkSync(foregroundAutoInvalidationPath)
    } catch {
      // Keep the sentinel when it cannot be removed; rejecting after restart
      // is safer than restoring an invalidated active marker.
    }
  }

  const issueForegroundAutoMarker = (sessionID: string): ForegroundAutoMarker => {
    if (foregroundAutoMarker) retireForegroundAutoMarker(foregroundAutoMarker.nonce, "expired")
    const marker = {
      sessionID,
      nonce: randomBytes(16).toString("hex"),
      expiresAt: Date.now() + foregroundAutoMarkerTtlMs,
    }
    foregroundAutoMarker = marker
    const timer = setTimeout(() => expireForegroundAutoMarker(marker.nonce), marker.expiresAt - Date.now())
    const unref = (timer as unknown as { unref?: () => void }).unref
    if (typeof unref === "function") unref.call(timer)
    foregroundAutoMarkerTimer = timer
    persistForegroundAutoProvenance()
    return marker
  }

  const scheduleForegroundAutoTombstoneExpiry = (entry: ForegroundAutoTombstone) => {
    const delay = Math.max(0, (entry.expiresAt ?? (Date.now() + foregroundAutoTombstoneTtlMs)) - Date.now())
    const timer = setTimeout(() => {
      foregroundAutoTombstones = foregroundAutoTombstones.filter((candidate) => candidate.nonce !== entry.nonce)
      persistForegroundAutoProvenance()
    }, delay)
    const unref = (timer as unknown as { unref?: () => void }).unref
    if (typeof unref === "function") unref.call(timer)
  }

  const rememberForegroundAutoTombstone = (marker: ForegroundAutoMarker, status: "expired" | "consumed"): boolean => {
    const tombstone: ForegroundAutoTombstone = {
      ...marker,
      status,
      expiresAt: Date.now() + foregroundAutoTombstoneTtlMs,
    }
    const nextTombstones = [
      ...foregroundAutoTombstones.filter((entry) => entry.nonce !== marker.nonce),
      tombstone,
    ]
    if (nextTombstones.length > foregroundAutoTombstoneLimit) {
      foregroundAutoProvenanceIncompleteUntil = Math.max(
        foregroundAutoProvenanceIncompleteUntil,
        Date.now() + foregroundAutoTombstoneTtlMs,
      )
    }
    foregroundAutoTombstones = nextTombstones.slice(-foregroundAutoTombstoneLimit)
    scheduleForegroundAutoTombstoneExpiry(tombstone)
    return persistForegroundAutoProvenance()
  }

  const retireForegroundAutoMarker = (nonce: string, status: "expired" | "consumed"): boolean => {
    const marker = foregroundAutoMarker
    if (!marker || marker.nonce !== nonce) return false
    const invalidationWritten = writeForegroundAutoInvalidation(marker, status)
    if (foregroundAutoMarkerTimer) clearTimeout(foregroundAutoMarkerTimer)
    foregroundAutoMarkerTimer = undefined
    foregroundAutoMarker = undefined
    if (foregroundAutoDispatch?.nonce === nonce) foregroundAutoDispatch = undefined
    const persisted = rememberForegroundAutoTombstone(marker, status)
    if (invalidationWritten && persisted) clearForegroundAutoInvalidation()
    else markForegroundAutoProvenanceUnavailable()
    return invalidationWritten && persisted
  }

  const expireForegroundAutoMarker = (nonce: string) => retireForegroundAutoMarker(nonce, "expired")

  const bindForegroundAutoMarker = (sessionID: string, messageID?: string) => {
    if (!messageID || foregroundAutoMarker?.sessionID !== sessionID) return
    foregroundAutoMarker.commandMessageID = messageID
    persistForegroundAutoProvenance()
  }

  const bindForegroundAutoMarkerToToolCall = async (sessionID: string, callID: string): Promise<"bound" | "manual" | "unrelated" | "mismatch" | "expired" | "consumed" | "unavailable"> => {
    const marker = foregroundAutoMarker
    // Keep unbound tombstones in the evidence set. A retired auto turn whose
    // host message was never observed is still provenance evidence, and an
    // unresolved tool call must not fall through to a manual checkpoint.
    const tombstones = foregroundAutoTombstones.filter((entry) => entry.sessionID === sessionID)
    const currentMarker = marker?.sessionID === sessionID && Boolean(marker.commandMessageID) ? marker : undefined
    const provenanceIncomplete = foregroundAutoProvenanceUnavailable
      || foregroundAutoProvenanceIncompleteUntil > Date.now()
    if (!currentMarker && tombstones.length === 0 && !provenanceIncomplete) return "unrelated"

    // Reserve the active marker synchronously before any await. A duplicate
    // tool call arriving while the history lookup is in flight must not see
    // an unbound marker and later fall through as manual.
    if (currentMarker?.bindingToolCallID) return "mismatch"
    if (currentMarker?.toolCallID && currentMarker.toolCallID !== callID) return "mismatch"
    if (currentMarker?.toolCallID === callID && currentMarker.restoredBoundCallID !== callID) return "mismatch"
    if (currentMarker) {
      // A persisted bound call may resume exactly once after plugin reload;
      // subsequent calls with the same ID are duplicates in this process.
      if (currentMarker.restoredBoundCallID === callID) currentMarker.restoredBoundCallID = undefined
      currentMarker.bindingToolCallID = callID
    }
    const finish = (result: "bound" | "manual" | "unrelated" | "mismatch" | "expired" | "consumed" | "unavailable") => {
      if (currentMarker?.bindingToolCallID === callID) currentMarker.bindingToolCallID = undefined
      return result
    }

    // Resolve a tool call against tombstones before considering the active
    // marker. A late call from an older auto turn must never be allowed to
    // retire or bind a newer marker that happens to be active in the same
    // session.
    const session = client.session as unknown as {
      messages?: (input: { path: { id: string }; query?: { limit?: number } }) => Promise<unknown>
    }
    if (typeof session.messages !== "function") return finish("unavailable")
    let matchingEntry: Json | undefined
    try {
      const response = await session.messages({ path: { id: sessionID }, query: { limit: foregroundAutoHistoryLimit } })
      const messagesValue = (response as Json | undefined)?.data ?? response
      if (!Array.isArray(messagesValue)) return finish("unavailable")
      matchingEntry = [...messagesValue].reverse().find((candidate) => {
        if (!candidate || typeof candidate !== "object") return false
        const parts = (candidate as Json).parts
        return Array.isArray(parts) && parts.some((part) => part && typeof part === "object" && stringField(part, "callID", "callId") === callID)
      }) as Json | undefined
      // A full page is only incomplete when this call is absent. The current
      // tool can legitimately be the last item in an exactly-full page.
      if (!matchingEntry && messagesValue.length >= foregroundAutoHistoryLimit) return finish("unavailable")
    } catch {
      return finish("unavailable")
    }

    if (matchingEntry) {
      const info = matchingEntry.info
      if (!info || typeof info !== "object") return finish("unavailable")
      const parentID = stringField(info, "parentID", "parentId")
      const lateMarker = tombstones.find((candidate) => candidate.commandMessageID === parentID)
      if (lateMarker) return finish(lateMarker.status)
      const knownManualParent = parentID === manualControlMessageID
      if (!currentMarker) {
        if (knownManualParent) manualCheckpointMessageID = stringField(info, "id", "messageID", "messageId")
        return finish(knownManualParent ? "manual" : "unavailable")
      }
      if (parentID !== currentMarker.commandMessageID) {
        if (knownManualParent) {
          manualCheckpointMessageID = stringField(info, "id", "messageID", "messageId")
          // A delayed manual control call wins the race with the active auto
          // turn. Retire the auto marker before returning manual provenance so
          // the delayed auto call cannot issue a duplicate checkpoint.
          const retired = retireForegroundAutoMarker(currentMarker.nonce, "expired")
          if (!retired) return finish("unavailable")
        }
        return finish(knownManualParent ? "manual" : "unavailable")
      }
    } else if (!currentMarker) {
      // A retained tombstone exists, but the bounded history did not contain
      // this call. Do not reinterpret an unresolvable auto call as manual.
      return finish("unavailable")
    }

    if (!currentMarker) return finish("unrelated")
    if (!matchingEntry) return finish("unavailable")
    const info = matchingEntry.info
    if (!info || typeof info !== "object") return finish("unavailable")
    const messageID = stringField(info, "id", "messageID", "messageId")
    const parentID = stringField(info, "parentID", "parentId")
    if (!messageID || parentID !== currentMarker.commandMessageID) return finish("unavailable")
    if (foregroundAutoMarker !== currentMarker || currentMarker.bindingToolCallID !== callID) return finish("mismatch")
    currentMarker.toolCallID = callID
    currentMarker.toolMessageID = messageID
    persistForegroundAutoProvenance()
    return finish("bound")
  }

  const consumeForegroundAutoMarker = (sessionID: string, messageID?: string): boolean => {
    const marker = foregroundAutoMarker
    if (!marker || marker.sessionID !== sessionID || !marker.toolCallID || !marker.toolMessageID || marker.toolMessageID !== messageID) return false
    return retireForegroundAutoMarker(marker.nonce, "consumed")
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
    if (foregroundAutoProvenanceUnavailable) {
      throw new Error("自动 checkpoint provenance 无法持久化；拒绝调度。")
    }
    const session = client.session as unknown as {
      command?: (input: { path: { id: string }; body: { command: string; arguments: string } }) => Promise<unknown>
    }
    if (typeof session.command === "function") {
      const dispatch = { sessionID, nonce: marker.nonce, partToken: randomBytes(32).toString("hex") }
      foregroundAutoDispatch = dispatch
      try {
        await session.command({
          path: { id: sessionID },
          // The nonce is an ephemeral host-to-host dispatch token. It is
          // stripped by command.execute.before and is never persisted or
          // placed in agent-visible message metadata.
          body: { command: "compact", arguments: `${foregroundAutoArgumentPrefix}${marker.nonce}` },
        })
      } catch (error) {
        if (foregroundAutoDispatch === dispatch) foregroundAutoDispatch = undefined
        throw error
      }
      return
    }
    throw new Error("OpenCode session command API is unavailable; refusing an unregistered auto prompt")
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

  const autoNonceFromArguments = (argumentsValue: string): string | undefined => {
    const value = argumentsValue.trim()
    return value.startsWith(foregroundAutoArgumentPrefix)
      ? value.slice(foregroundAutoArgumentPrefix.length) || undefined
      : undefined
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

  const restoreForegroundAutoProvenance = () => {
    const mainPresent = existsSync(foregroundAutoProvenancePath)
    const invalidationPresent = existsSync(foregroundAutoInvalidationPath)
    if (!mainPresent && !invalidationPresent) return
    try {
      // A retirement sentinel is independently authoritative when the main
      // mirror was lost between the state mutation and its write.
      const stored = mainPresent
        ? JSON.parse(readFileSync(foregroundAutoProvenancePath, "utf8")) as Json
        : { marker: null, tombstones: [], incompleteUntil: 0 } as Json
      const now = Date.now()
      // Load the persisted fail-closed window before any normalization write;
      // otherwise dirty recovery can overwrite it with zero.
      foregroundAutoProvenanceIncompleteUntil = Math.max(0, Number(stored.incompleteUntil) || 0)
      let dirty = false
      const restoreMarker = (value: unknown, status?: "expired" | "consumed"): ForegroundAutoMarker | ForegroundAutoTombstone | undefined => {
        if (!value || typeof value !== "object") return undefined
        const record = value as Json
        const sessionID = stringField(record, "sessionID")
        // Nonces are process-local dispatch secrets. Persisted tombstones can
        // be rehydrated with a fresh internal nonce because they are matched
        // by host message/tool provenance; an active marker still needs its
        // host command message to be usable after restart.
        const nonce = randomBytes(16).toString("hex")
        if (!sessionID || (!status && !stringField(record, "commandMessageID"))) return undefined
        const expiryValue = record.expiresAt
        const validExpiry = typeof expiryValue === "number" && Number.isFinite(expiryValue) && expiryValue > 0
        // An active marker with malformed expiry is not safe to restore: its
        // authorization lifetime cannot be established. Compatibility TTL
        // fallback is limited to already-retired tombstones.
        if (!status && !validExpiry) return undefined
        const expiresAt = validExpiry
          ? expiryValue as number
          : now + foregroundAutoTombstoneTtlMs
        const marker: ForegroundAutoMarker = {
          sessionID,
          nonce,
          expiresAt,
          commandMessageID: stringField(record, "commandMessageID"),
          toolCallID: stringField(record, "toolCallID"),
          toolMessageID: stringField(record, "toolMessageID"),
          restoredBoundCallID: !status ? stringField(record, "toolCallID") : undefined,
        }
        return status ? { ...marker, status } : marker
      }
      let invalidStructure = false
      const hasOwn = (key: string) => Object.prototype.hasOwnProperty.call(stored, key)
      if (mainPresent && (!hasOwn("marker") || !hasOwn("tombstones") || !Array.isArray(stored.tombstones))) invalidStructure = true
      const storedMarker = stored.marker === null
        ? undefined
        : restoreMarker(stored.marker)
      if (mainPresent && stored.marker !== null && !storedMarker) invalidStructure = true
      let restoredInvalidation: ForegroundAutoTombstone | undefined
      let invalidationExpired = false
      if (existsSync(foregroundAutoInvalidationPath)) {
        try {
          const invalidation = JSON.parse(readFileSync(foregroundAutoInvalidationPath, "utf8")) as Json
          const status = invalidation.status
          if (status !== "expired" && status !== "consumed") invalidStructure = true
          else {
            const restored = restoreMarker(invalidation, status)
            if (!restored || !("status" in restored)) invalidStructure = true
            else if ((restored.expiresAt ?? 0) > now) restoredInvalidation = restored
            else invalidationExpired = true
          }
        } catch {
          invalidStructure = true
        }
      }
      const invalidatesStoredMarker = Boolean(
        restoredInvalidation
        && storedMarker
        && !("status" in storedMarker)
        && restoredInvalidation.sessionID === storedMarker.sessionID
        && ((restoredInvalidation.commandMessageID && restoredInvalidation.commandMessageID === storedMarker.commandMessageID)
          || (restoredInvalidation.toolCallID && restoredInvalidation.toolCallID === storedMarker.toolCallID)),
      )
      if (storedMarker && !("status" in storedMarker) && (storedMarker.expiresAt ?? 0) > now && !invalidatesStoredMarker) {
        foregroundAutoMarker = storedMarker
        const timer = setTimeout(() => expireForegroundAutoMarker(storedMarker.nonce), (storedMarker.expiresAt ?? now) - now)
        const unref = (timer as unknown as { unref?: () => void }).unref
        if (typeof unref === "function") unref.call(timer)
        foregroundAutoMarkerTimer = timer
      } else if (storedMarker) {
        // An active marker that expired while the plugin was offline is still
        // evidence of an auto control turn. Preserve it as a tombstone so a
        // delayed tool call cannot be reinterpreted as manual.
        dirty = true
      }
      const storedTombstones = Array.isArray(stored.tombstones) ? stored.tombstones : []
      const hasPersistedNonce = (value: unknown): boolean => Boolean(
        value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "nonce"),
      )
      const restoredExpiredMarker = storedMarker && !("status" in storedMarker) && (storedMarker.expiresAt ?? 0) <= now
        ? { ...storedMarker, status: "expired" as const, expiresAt: now + foregroundAutoTombstoneTtlMs }
        : undefined
      const restoredTombstones = storedTombstones
        .map((entry) => {
          const status = entry && typeof entry === "object" && (entry as Json).status
          if (status !== "expired" && status !== "consumed") {
            invalidStructure = true
            return undefined
          }
          const restored = restoreMarker(entry, status)
          if (!restored || !("status" in restored)) invalidStructure = true
          return restored
        })
        .filter((entry): entry is ForegroundAutoTombstone => Boolean(entry && "status" in entry && (entry.expiresAt ?? 0) > now))
      foregroundAutoTombstones = [
        ...(restoredInvalidation ? [restoredInvalidation] : []),
        ...(restoredExpiredMarker ? [restoredExpiredMarker] : []),
        ...restoredTombstones,
      ].filter((entry, index, entries) => entries.findIndex((candidate) => candidate.nonce === entry.nonce) === index)
        .slice(-foregroundAutoTombstoneLimit)
      if (!mainPresent
        || foregroundAutoTombstones.length !== storedTombstones.length
        || restoredExpiredMarker
        || restoredInvalidation
        || invalidationExpired
        || hasPersistedNonce(stored.marker)
        || storedTombstones.some(hasPersistedNonce)
        || invalidStructure) dirty = true
      for (const entry of foregroundAutoTombstones) scheduleForegroundAutoTombstoneExpiry(entry)
      if (invalidStructure) markForegroundAutoProvenanceUnavailable()
      const persisted = dirty ? persistForegroundAutoProvenance() : true
      if ((restoredInvalidation || invalidationExpired) && persisted) clearForegroundAutoInvalidation()
      // persistForegroundAutoProvenance clears the transient flag on success;
      // malformed state must remain unavailable after the normalization write.
      if (invalidStructure) foregroundAutoProvenanceUnavailable = true
    } catch {
      // A malformed or unavailable mirror cannot authorize a call. The
      // absence of restored provenance therefore remains fail-closed.
      markForegroundAutoProvenanceUnavailable()
    }
  }

  restoreForegroundAutoProvenance()

  return {
    tool: {
      pco_checkpoint: tool({
        description: "Run the PCO checkpoint. Call exactly once for /compact; the host decides whether this is manual or an authorized foreground auto trigger.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO checkpoint 只能由主 session 执行。")
          context.metadata({ title: "PCO checkpoint" })
          const contextMessageID = stringField(context, "messageID", "messageId")
          const contextCallID = stringField(context, "callID", "callId", "toolCallID", "toolCallId")
          const knownManualProvenance = manualCheckpointSessionID === context.sessionID
            && ((contextCallID && manualCheckpointCallID === contextCallID)
              || (contextMessageID && manualCheckpointMessageID === contextMessageID))
          if (!knownManualProvenance && foregroundAutoMarker?.sessionID === context.sessionID
            && !foregroundAutoMarker.toolCallID) {
            retireForegroundAutoMarker(foregroundAutoMarker.nonce, "expired")
            throw new Error("自动 checkpoint 的工具调用缺少 host provenance；拒绝降级为 manual。")
          }
          if (!knownManualProvenance && foregroundAutoMarker?.sessionID === context.sessionID
            && foregroundAutoMarker.toolCallID
            && foregroundAutoMarker.toolMessageID !== contextMessageID) {
            retireForegroundAutoMarker(foregroundAutoMarker.nonce, "expired")
            throw new Error("自动 checkpoint 的工具消息身份不匹配；拒绝降级为 manual。")
          }
          const autoRetirementExpected = !knownManualProvenance
            && foregroundAutoMarker?.sessionID === context.sessionID
            && Boolean(foregroundAutoMarker.toolCallID)
            && foregroundAutoMarker.toolMessageID === contextMessageID
          let trigger: "auto" | "manual"
          if (knownManualProvenance) {
            trigger = "manual"
          } else {
            const consumed = consumeForegroundAutoMarker(context.sessionID, contextMessageID)
            if (autoRetirementExpected && !consumed) {
              throw new Error("自动 checkpoint provenance retirement 未持久化；拒绝执行。")
            }
            trigger = consumed ? "auto" : "manual"
          }
          if (knownManualProvenance) {
            manualCheckpointSessionID = undefined
            manualCheckpointCallID = undefined
            manualCheckpointMessageID = undefined
          }
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
        description: "Read the current PCO checkpoint status without changing it. If approval is required, call the native question tool in this same turn.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO 状态恢复只能由主 session 执行。")
          const result = await invoke(["checkpoint", "status"], context.sessionID)
          await rehydrateApproval(context.sessionID, result)
          return JSON.stringify(result)
        },
      }),
      pco_retry: tool({
        description: "Retry checkpoint recovery or any pending post-commit derivations from their durable boundary. If a proposal is returned, call the native question tool in this same turn.",
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
      const autoMarker = foregroundAutoMarker
      const hasAutoProvenance = autoMarker?.sessionID === input.sessionID
        || foregroundAutoTombstones.some((entry) => entry.sessionID === input.sessionID)
        || foregroundAutoProvenanceUnavailable
        || foregroundAutoProvenanceIncompleteUntil > Date.now()
      if (input.tool === "pco_checkpoint" && hasAutoProvenance) {
        const provenance = await bindForegroundAutoMarkerToToolCall(input.sessionID, input.callID)
        if (foregroundAutoProvenanceUnavailable && provenance !== "manual") {
          throw new Error("自动 checkpoint provenance 无法持久化；拒绝执行。")
        }
        if (provenance === "mismatch" || provenance === "expired" || provenance === "consumed") {
          throw new Error("自动 checkpoint 出现重复或不匹配的工具调用；拒绝执行。")
        }
        if (provenance === "manual") {
          manualCheckpointSessionID = input.sessionID
          manualCheckpointCallID = input.callID
        }
        if (provenance === "unavailable") {
          // The lookup may belong to an older tombstone while a newer marker
          // is active. Keep the marker intact; the call is rejected, and a
          // later retry can still establish the newer marker's provenance.
          throw new Error("自动 checkpoint 的工具 provenance 不可用；拒绝降级为 manual。")
        }
      }
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
      const commandAutoNonce = autoNonceFromArguments(input.arguments)
      const scheduledAutoDispatch = commandAutoNonce
        && foregroundAutoDispatch?.sessionID === input.sessionID
        && foregroundAutoDispatch.nonce === commandAutoNonce
        && foregroundAutoMarker?.sessionID === input.sessionID
        && foregroundAutoMarker.nonce === commandAutoNonce
        ? foregroundAutoDispatch
        : undefined
      if (commandAutoNonce && !scheduledAutoDispatch) {
        throw new Error("自动 checkpoint provenance 已过期或不匹配；拒绝执行。")
      }
      if (input.command === "compact" || recoveryCommands.has(input.command)) {
        const marker = foregroundAutoMarker
        if (marker?.sessionID === input.sessionID) {
          if (recoveryCommands.has(input.command)) {
            // Explicit recovery commands cancel any delayed auto control turn
            // before they can mutate the checkpoint again.
            retireForegroundAutoMarker(marker.nonce, "expired")
          } else {
            const autoPart = Boolean(scheduledAutoDispatch) && scheduledAutoDispatch === foregroundAutoDispatch
            if (autoPart) {
              scheduledAutoDispatch.commandObserved = true
              for (const part of output.parts) {
                if (part.type !== "text") continue
                const jsonPart = part as unknown as Json
                jsonPart.metadata = {
                  ...((jsonPart.metadata as Json | undefined) ?? {}),
                  pco_auto_control: true,
                  // Unlike the scheduler nonce, this token is never placed
                  // in command text or persisted. It exists only for the
                  // host-to-host command/message binding and survives the
                  // SDK's part cloning boundary.
                  pco_auto_dispatch_token: scheduledAutoDispatch.partToken,
                }
                if (typeof jsonPart.text === "string") {
                  jsonPart.text = jsonPart.text.split(`${foregroundAutoArgumentPrefix}${marker.nonce}`).join("")
                }
              }
            } else if (marker.commandMessageID) {
              if (marker.toolCallID) throw new Error("自动 checkpoint control turn 已经绑定工具调用。")
              // A later, explicit /compact is a manual recovery command. Keep a
              // tombstone for the old auto message so a late tool call cannot be
              // reinterpreted as manual, then let this command proceed.
              retireForegroundAutoMarker(marker.nonce, "expired")
            } else {
              // A user command raced the plugin's pending auto command. Do not
              // attach the auto nonce to it; retire the nonce so the eventual
              // stale auto command is rejected instead of becoming manual.
              retireForegroundAutoMarker(marker.nonce, "expired")
            }
          }
        }
        if (input.command === "compact" && !scheduledAutoDispatch) {
          manualCommandPendingSessionID = input.sessionID
          manualControlMessageID = undefined
        }
      }
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
                retireForegroundAutoMarker(marker.nonce, "expired")
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
      const marker = foregroundAutoMarker
      const autoControlPart = output.parts.find((part) => {
        if (part.type !== "text") return false
        const metadata = (part as unknown as Json).metadata as Json | undefined
        return metadata?.pco_auto_control === true
      })
      const messageID = stringField(output.message, "id", "messageID", "messageId")
      const autoDispatch = foregroundAutoDispatch?.sessionID === input.sessionID
        && foregroundAutoDispatch.commandObserved
        ? foregroundAutoDispatch
        : undefined
      const dispatchPart = autoDispatch && output.parts.some((part) => {
        if (part.type !== "text") return false
        const metadata = (part as unknown as Json).metadata as Json | undefined
        return metadata?.pco_auto_dispatch_token === autoDispatch.partToken
      })
      const activeAuto = Boolean(autoControlPart)
        && Boolean(autoDispatch)
        && Boolean(dispatchPart)
        && marker?.sessionID === input.sessionID
        && marker.nonce === autoDispatch?.nonce
      const staleAuto = autoControlPart
        ? foregroundAutoTombstones.find((entry) => entry.sessionID === input.sessionID && entry.commandMessageID === messageID)
        : undefined
      if (staleAuto) {
        throw new Error(`自动 checkpoint provenance 已${staleAuto.status === "consumed" ? "消费" : "过期"}；拒绝该 control turn。`)
      }
      if (autoControlPart && !activeAuto) {
        // Auto control metadata is host-produced. Without the matching
        // in-memory scheduler dispatch, it is either a replay or a forged
        // message and must never be treated as a manual turn.
        throw new Error("自动 checkpoint provenance nonce 未知或已失效；拒绝该 control turn。")
      }
      // A delayed manual command may overlap an auto control message. The
      // auto message must not consume the pending manual binding; otherwise
      // the real manual message will be rejected when its tool call arrives.
      if (manualCommandPendingSessionID === input.sessionID && !autoControlPart) {
        manualControlMessageID = stringField(output.message, "id", "messageID", "messageId")
        manualCommandPendingSessionID = undefined
      }
      if (marker?.sessionID === input.sessionID && !marker.commandMessageID) {
        const autoPart = activeAuto
        if (autoPart) {
          if (!messageID) {
            retireForegroundAutoMarker(marker.nonce, "expired")
            throw new Error("自动 checkpoint 的 host message provenance 不可用；拒绝降级为 manual。")
          }
          bindForegroundAutoMarker(input.sessionID, messageID)
          foregroundAutoDispatch = undefined
        } else {
          // A host message without the nonce won the race with the pending
          // auto prompt. Retire the auto attempt and let the real user turn
          // remain manual; a later stale auto prompt is rejected by its
          // tombstone above.
          retireForegroundAutoMarker(marker.nonce, "expired")
        }
      }
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
- After pco_status or pco_retry returns an awaiting proposal, call the native question tool in the same control turn; the plugin must not submit a nested session prompt.
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
