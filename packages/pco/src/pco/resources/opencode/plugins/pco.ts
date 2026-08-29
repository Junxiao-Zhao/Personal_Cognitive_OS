import { existsSync, mkdirSync, readFileSync, readdirSync, unlinkSync, writeFileSync } from "node:fs"
import { resolve } from "node:path"
import { createHash, createHmac, randomBytes } from "node:crypto"
import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"

type Json = Record<string, unknown>
type CheckpointIntent = "consolidate" | "compact"
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
  intent: CheckpointIntent
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
  intent: CheckpointIntent
  // This process-local token is copied into command-part metadata by the
  // host hook. OpenCode clones parts between command execution and
  // chat.message, so object identity cannot be used as the binding.
  partToken: string
  commandObserved?: boolean
}
type PendingCompaction = {
  requestID: string
  eventID?: string
  sessionID: string
  requestedBoundary?: string
  requestedAt: number
  origin: "harness_auto_compaction"
}
type NativeCompactBypass = {
  token: string
  checkpointID: string
  sessionID: string
  attemptID: string
  expiresAt: number
  consumed?: boolean
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
  const pendingCompactionPath = resolve(stateRoot, "pending-compaction.json")
  const nativeCompactBypassPath = resolve(stateRoot, "native-compact-bypass.json")
  const receiptInboxPath = resolve(stateRoot, "receipt-inbox")
  const receiptInboxIndexPath = resolve(receiptInboxPath, "index.json")
  mkdirSync(stateRoot, { recursive: true })
  mkdirSync(receiptInboxPath, { recursive: true })
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
  let manualCommandPendingIntent: CheckpointIntent | undefined
  let manualControlMessageID: string | undefined
  let manualControlIntent: CheckpointIntent | undefined
  let manualCheckpointSessionID: string | undefined
  let manualCheckpointCallID: string | undefined
  let manualCheckpointMessageID: string | undefined
  let manualCheckpointIntent: CheckpointIntent | undefined
  let pendingCompaction: PendingCompaction | undefined
  let nativeCompactBypass: NativeCompactBypass | undefined
  const foregroundAutoMarkerTtlMs = 5 * 60_000
  const foregroundAutoTombstoneTtlMs = 10 * 60_000
  const foregroundAutoHistoryLimit = 10_000
  const foregroundAutoArgumentPrefix = "--pco-auto-nonce="
  const foregroundAutoTombstoneLimit = 64
  const checkpointCommands = new Map<string, CheckpointIntent>([["compact", "compact"], ["consolidate", "consolidate"]])
  const controlCommands = new Set(["compact", "consolidate", "pco-abort", "pco-retry", "pco-status"])
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

  const issueForegroundAutoMarker = (sessionID: string, intent: CheckpointIntent): ForegroundAutoMarker => {
    if (foregroundAutoMarker) retireForegroundAutoMarker(foregroundAutoMarker.nonce, "expired")
    const marker = {
      sessionID,
      nonce: randomBytes(16).toString("hex"),
      intent,
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
        if (knownManualParent) {
          manualCheckpointMessageID = stringField(info, "id", "messageID", "messageId")
          manualCheckpointIntent = manualControlIntent
        }
        return finish(knownManualParent ? "manual" : "unavailable")
      }
      if (parentID !== currentMarker.commandMessageID) {
        if (knownManualParent) {
          manualCheckpointMessageID = stringField(info, "id", "messageID", "messageId")
          manualCheckpointIntent = manualControlIntent
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

  const writeJson = (path: string, value: unknown): boolean => {
    try {
      writeFileSync(path, JSON.stringify(value), "utf8")
      return true
    } catch {
      return false
    }
  }

  const readJson = (path: string): Json | undefined => {
    if (!existsSync(path)) return undefined
    try {
      const value = JSON.parse(readFileSync(path, "utf8"))
      return value && typeof value === "object" ? value as Json : undefined
    } catch {
      return undefined
    }
  }

  const drainReceiptInbox = async (): Promise<void> => {
    const index = readJson(receiptInboxIndexPath) ?? {}
    let changed = false
    for (const name of readdirSync(receiptInboxPath)) {
      if (!name.endsWith(".json") || name === "index.json") continue
      const record = readJson(resolve(receiptInboxPath, name))
      if (!record) continue
      const key = stringField(record, "key")
      const payloadHash = stringField(record, "payload_hash", "payloadHash")
      const generation = typeof record.generation === "number" ? record.generation : undefined
      const prior = key ? (index[key] as Json | undefined) : undefined
      if (!key || !payloadHash || generation === undefined) continue
      if (prior && prior.payload_hash !== payloadHash) {
        await client.app.log({ body: {
          service: "pco",
          level: "error",
          message: "PCO receipt inbox key conflict; refusing to acknowledge a different payload.",
          extra: { code: "RECEIPT_KEY_CONFLICT", key },
        } })
        continue
      }
      if (prior && Number(prior.generation ?? -1) >= generation) continue
      index[key] = {
        host_resource_id: record.host_resource_id,
        key,
        generation,
        payload_hash: payloadHash,
        disposition: prior ? "replaced" : "created",
        acknowledged_at: Date.now(),
      }
      changed = true
      const payload = record.payload as Json | undefined
      await client.app.log({ body: {
        service: "pco",
        level: "info",
        message: String(payload?.summary ?? "PCO checkpoint completed"),
        extra: { receipt_key: key, generation, supersedes_key: record.supersedes_key ?? null },
      } })
    }
    if (changed && !writeJson(receiptInboxIndexPath, index)) {
      throw new Error("PCO receipt inbox acknowledgement could not be persisted")
    }
  }

  const persistPendingCompaction = (value: PendingCompaction | undefined): boolean => {
    if (!value) {
      try {
        if (existsSync(pendingCompactionPath)) unlinkSync(pendingCompactionPath)
        return true
      } catch {
        return false
      }
    }
    return writeJson(pendingCompactionPath, value)
  }

  const pendingCompactionArgs = (value: PendingCompaction): string[] => [
    "--pending-compaction-json",
    JSON.stringify({
      request_id: value.requestID,
      event_id: value.eventID ?? null,
      session_id: value.sessionID,
      requested_boundary: value.requestedBoundary ?? null,
      requested_at: value.requestedAt,
      origin: value.origin,
    }),
  ]

  const pendingCompactionRetiredByPython = (result: Json): boolean => {
    const checkpoint = result.checkpoint as Json | undefined
    // Compatibility with older loopback/CLI receipts that predate the
    // authoritative checkpoint echo. A compact-completed receipt is safe to
    // use only when it explicitly identifies compact intent; a consolidate
    // receipt never retires the marker.
    if (!checkpoint) {
      const compaction = result.compaction as Json | undefined
      return result.intent === "compact" && compaction?.status === "completed"
    }
    return checkpoint?.pending_compaction == null
      && checkpoint?.compaction_status === "completed"
      && checkpoint?.receipt_inserted === true
      && checkpoint?.input_unlocked === true
  }

  const persistNativeCompactBypass = (value: NativeCompactBypass | undefined): boolean => {
    if (!value) {
      try {
        if (existsSync(nativeCompactBypassPath)) unlinkSync(nativeCompactBypassPath)
        return true
      } catch {
        return false
      }
    }
    return writeJson(nativeCompactBypassPath, value)
  }

  const restoreHarnessGateState = () => {
    const pending = readJson(pendingCompactionPath)
    if (pending
      && typeof pending.requestID === "string"
      && typeof pending.sessionID === "string"
      && pending.origin === "harness_auto_compaction"
      && typeof pending.requestedAt === "number") {
      pendingCompaction = pending as unknown as PendingCompaction
    }
    // A bypass is one operation's handshake, not a restartable capability.
    // Fail closed on Plugin reload so an unconsumed token cannot authorize a
    // later compact after the original request/attempt has disappeared.
    nativeCompactBypass = undefined
    if (existsSync(nativeCompactBypassPath)) persistNativeCompactBypass(undefined)
  }

  const mintNativeCompactBypass = (checkpointID: string, sessionID: string, attemptID: string): NativeCompactBypass | undefined => {
    const existing = readJson(nativeCompactBypassPath)
    if (existing
      && typeof existing.token === "string"
      && typeof existing.checkpointID === "string"
      && typeof existing.sessionID === "string"
      && typeof existing.attemptID === "string"
      && typeof existing.expiresAt === "number"
      && Number.isFinite(existing.expiresAt)
      && Number.isInteger(existing.expiresAt)
      && existing.expiresAt > Date.now()
      && existing.consumed !== true
      && existing.checkpointID === checkpointID
      && existing.sessionID === sessionID
      && existing.attemptID === attemptID) {
      nativeCompactBypass = existing as unknown as NativeCompactBypass
      return nativeCompactBypass
    }
    if (existing) persistNativeCompactBypass(undefined)
    const value: NativeCompactBypass = {
      token: randomBytes(32).toString("hex"),
      checkpointID,
      sessionID,
      attemptID,
      expiresAt: Date.now() + 5 * 60_000,
    }
    if (!persistNativeCompactBypass(value)) return undefined
    nativeCompactBypass = value
    return value
  }

  const fieldFromInput = (input: unknown, ...fields: string[]): string | undefined => {
    const record = input && typeof input === "object" ? input as Json : undefined
    const nested = (record?.properties as Json | undefined) ?? (record?.metadata as Json | undefined)
    return stringField(record, ...fields) ?? stringField(nested, ...fields)
  }

  const compactionTokenFromInput = (input: unknown): NativeCompactBypass | undefined => {
    const record = input && typeof input === "object" ? input as Json : undefined
    const metadata = (record?.metadata as Json | undefined) ?? (record?.properties as Json | undefined) ?? {}
    const candidate = (record?.pco_native_compact as Json | undefined)
      ?? (record?.pcoNativeCompact as Json | undefined)
      ?? (metadata.pco_native_compact as Json | undefined)
      ?? (metadata.pcoNativeCompact as Json | undefined)
    if (!candidate || typeof candidate !== "object") return undefined
    const token = stringField(candidate, "token", "bypassToken")
    const checkpointID = stringField(candidate, "checkpointID", "checkpointId", "checkpoint_id")
    const sessionID = stringField(candidate, "sessionID", "sessionId", "session_id")
    const attemptID = stringField(candidate, "attemptID", "attemptId", "attempt_id")
    const expiresAtValue = candidate.expiresAt ?? candidate.expires_at
    const expiresAt = typeof expiresAtValue === "number"
      && Number.isFinite(expiresAtValue)
      && Number.isInteger(expiresAtValue)
      ? expiresAtValue
      : undefined
    if (!token || !checkpointID || !sessionID || !attemptID || expiresAt === undefined) return undefined
    return { token, checkpointID, sessionID, attemptID, expiresAt }
  }

  const consumeNativeCompactBypass = (input: unknown): boolean => {
    const candidate = compactionTokenFromInput(input)
    // The Python adapter binds the pending token to the real checkpoint ID
    // immediately before POST /summarize. Re-read the shared durable file so
    // the hook cannot compare against a stale process-local "pending" value.
    const persisted = readJson(nativeCompactBypassPath)
    if (persisted
      && typeof persisted.token === "string"
      && typeof persisted.checkpointID === "string"
      && typeof persisted.sessionID === "string"
      && typeof persisted.attemptID === "string"
      && typeof persisted.expiresAt === "number"
      && Number.isFinite(persisted.expiresAt)
      && Number.isInteger(persisted.expiresAt)
      && persisted.expiresAt > Date.now()
      && persisted.consumed !== true) {
      nativeCompactBypass = persisted as unknown as NativeCompactBypass
    } else if (existsSync(nativeCompactBypassPath)) {
      // Expired or malformed state cannot authorize a later request.
      persistNativeCompactBypass(undefined)
      nativeCompactBypass = undefined
    }
    const stored = nativeCompactBypass
    if (!candidate || !stored || stored.consumed === true || stored.expiresAt <= Date.now()) return false
    const matches = candidate.token === stored.token
      && candidate.checkpointID === stored.checkpointID
      && candidate.sessionID === stored.sessionID
      && candidate.attemptID === stored.attemptID
      && candidate.expiresAt === stored.expiresAt
      && stored.sessionID === fieldFromInput(input, "sessionID", "sessionId")
    if (!matches) return false
    stored.consumed = true
    const retired = persistNativeCompactBypass(undefined)
    nativeCompactBypass = undefined
    return retired
  }

  const retireNativeCompactBypass = () => {
    nativeCompactBypass = undefined
    if (!persistNativeCompactBypass(undefined)) {
      throw new Error("PCO native compact bypass could not be retired")
    }
  }

  const invokeWithNativeCompactBypass = async (
    args: string[],
    sessionID: string,
    intent: CheckpointIntent,
  ): Promise<Json> => {
    if (intent !== "compact") return invoke(args, sessionID)
    // The token is created before the Python command starts. Python persists
    // the attempt binding and OpenCodeAdapter forwards this same token to the
    // real /summarize request; the hook therefore never needs a post-hoc
    // authorization window.
    let checkpointID = "pending"
    let attemptID = `compact_${randomBytes(16).toString("hex")}`
    try {
      const status = await invoke(["checkpoint", "status"], sessionID)
      const checkpoint = status.checkpoint as Json | undefined
      const checkpointStatus = typeof checkpoint?.status === "string" ? checkpoint.status : ""
      const activeCompact = checkpoint?.intent === "compact"
        && checkpoint?.compaction_status === "pending"
        && checkpointStatus !== "DONE"
        && checkpointStatus !== "COMMITTED_WITH_PENDING_DERIVATIONS"
        && checkpointStatus !== "ABORTED"
        && checkpointStatus !== "RECOVERY"
        && typeof checkpoint?.id === "string"
        && typeof checkpoint?.native_compact_attempt_id === "string"
        && Boolean(checkpoint.native_compact_attempt_id)
      // A terminal checkpoint ID is historical data, not the identity of this
      // new compact request. Only an active compact with a durable attempt may
      // reuse its binding; all other requests start with pending/new identity.
      if (activeCompact) {
        checkpointID = checkpoint.id as string
        attemptID = checkpoint.native_compact_attempt_id as string
      }
    } catch {
      // A new /compact has no active state yet; the generated attempt is
      // supplied to Python when it creates the durable checkpoint.
    }
    const bypass = mintNativeCompactBypass(checkpointID, sessionID, attemptID)
    if (!bypass) throw new Error("PCO native compact bypass could not be persisted")
    try {
      const result = await invoke([
        ...args,
        "--native-compact-token", bypass.token,
        "--native-compact-attempt-id", attemptID,
        "--native-compact-expires-at", String(bypass.expiresAt),
      ], sessionID)
      const checkpoint = result.checkpoint as Json | undefined
      const compactCompleted = checkpoint?.compaction_status === "completed"
        || (result.compaction as Json | undefined)?.status === "completed"
      // Requests that stop at an active/approval/recovery boundary did not
      // consume the native capability. Retire it immediately; only a
      // completed native compact may leave the handshake consumed by Python.
      if (!compactCompleted && (result.approval_required === true || result.pending_compaction_merged === true || checkpoint)) {
        retireNativeCompactBypass()
      }
      return result
    } catch (error) {
      retireNativeCompactBypass()
      throw error
    }
  }

  restoreHarnessGateState()
  await drainReceiptInbox()

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
      const dispatch = { sessionID, nonce: marker.nonce, intent: marker.intent, partToken: randomBytes(32).toString("hex") }
      foregroundAutoDispatch = dispatch
      try {
        await session.command({
          path: { id: sessionID },
          // The nonce is an ephemeral host-to-host dispatch token. It is
          // stripped by command.execute.before and is never persisted or
          // placed in agent-visible message metadata.
          body: {
            command: marker.intent,
            arguments: `${foregroundAutoArgumentPrefix}${marker.nonce}`,
          },
          // Legacy loopback contract marker: body: { command: "compact", arguments: ... }
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

  const bindManualCheckpointToToolCall = async (sessionID: string, callID: string): Promise<boolean> => {
    if ((!manualCommandPendingSessionID && !manualControlMessageID)
      || (manualCommandPendingSessionID && manualCommandPendingSessionID !== sessionID)
      || !manualControlMessageID
      || !manualControlIntent) return false
    const session = client.session as unknown as {
      messages?: (input: { path: { id: string }; query?: { limit?: number } }) => Promise<unknown>
    }
    if (typeof session.messages !== "function") return false
    try {
      const response = await session.messages({ path: { id: sessionID }, query: { limit: foregroundAutoHistoryLimit } })
      const messagesValue = ((response as Json | undefined)?.data ?? response)
      if (!Array.isArray(messagesValue)) return false
      const matchingEntry = [...messagesValue].reverse().find((candidate) => {
        if (!candidate || typeof candidate !== "object") return false
        const info = (candidate as Json).info
        const parts = (candidate as Json).parts
        return info && typeof info === "object"
          && stringField(info, "parentID", "parentId") === manualControlMessageID
          && Array.isArray(parts)
          && parts.some((part) => part && typeof part === "object" && stringField(part, "callID", "callId") === callID)
      })
      if (!matchingEntry) return false
      manualCheckpointSessionID = sessionID
      manualCheckpointCallID = callID
      manualCheckpointMessageID = stringField((matchingEntry as Json).info, "id", "messageID", "messageId")
      manualCheckpointIntent = manualControlIntent
      return true
    } catch {
      return false
    }
  }

  const autoNonceFromArguments = (argumentsValue: string): string | undefined => {
    const value = argumentsValue.trim()
    return value.startsWith(foregroundAutoArgumentPrefix)
      ? value.slice(foregroundAutoArgumentPrefix.length) || undefined
      : undefined
  }

  const autoIntentFromProbe = (probe: Json): CheckpointIntent | undefined => {
    // Compact wins when both thresholds are true because it includes
    // consolidation. The explicit intent is authoritative; the boolean
    // fields make the Plugin compatible with the split Phase 1 probe.
    if (probe.intent === "compact" || probe.auto_compact === true || probe.context_needed === true) return "compact"
    if (probe.intent === "consolidate" || probe.auto_consolidate === true || probe.new_public_material === true) return "consolidate"
    // v0.3 probes only exposed `needed`; preserve that as the old compact
    // behavior until the core starts returning the split fields.
    return probe.needed === true ? "compact" : undefined
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
        const expiryValue = record.expiresAt
        const expiredLegacyMarker = typeof expiryValue === "number" && Number.isFinite(expiryValue) && expiryValue <= now
        const intent = record.intent === "consolidate" || record.intent === "compact"
          ? record.intent
          : status || expiredLegacyMarker ? "compact" : undefined
        // Nonces are process-local dispatch secrets. Persisted tombstones can
        // be rehydrated with a fresh internal nonce because they are matched
        // by host message/tool provenance; an active marker still needs its
        // host command message to be usable after restart.
        const nonce = randomBytes(16).toString("hex")
        if (!sessionID || !intent || (!status && !stringField(record, "commandMessageID"))) return undefined
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
          intent,
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
  // Keep an explicit durable empty mirror so a host can distinguish “no
  // pending auto provenance” from a failed persistence attempt.
  if (!existsSync(foregroundAutoProvenancePath)) persistForegroundAutoProvenance()

  return {
    tool: {
      pco_checkpoint: tool({
        description: "Run the PCO checkpoint. Call exactly once from an authorized /consolidate, /compact, or host auto control turn. This tool has no model-selectable trigger or intent arguments.",
        args: {},
        async execute(_args, context) {
          if (!mainSession(context.sessionID)) throw new Error("PCO checkpoint 只能由主 session 执行。")
          if (Object.keys((_args as unknown as Json) ?? {}).length > 0) {
            throw new Error("pco_checkpoint 不接受 trigger 或 intent 参数；请使用 Host 绑定的 /consolidate 或 /compact provenance。")
          }
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
            if (!consumed) throw new Error("pco_checkpoint 缺少合法 Host provenance；拒绝普通 Agent 调用。")
            trigger = "auto"
          }
          const intent = knownManualProvenance
            ? manualCheckpointIntent
            : foregroundAutoTombstones.find((entry) => entry.sessionID === context.sessionID && entry.toolMessageID === contextMessageID)?.intent
          if (!intent) throw new Error("pco_checkpoint provenance 缺少 intent；拒绝执行。")
          if (knownManualProvenance) {
            manualCheckpointSessionID = undefined
            manualCheckpointCallID = undefined
            manualCheckpointMessageID = undefined
            manualCheckpointIntent = undefined
          }
          const result = await invokeWithNativeCompactBypass([
            "checkpoint", "request", "--trigger", trigger, "--intent", intent,
          ], context.sessionID, intent)
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
            const status = await invoke(["checkpoint", "status"], context.sessionID)
            const checkpoint = status.checkpoint as Json | undefined
            const intent: CheckpointIntent = checkpoint?.intent === "compact" ? "compact" : "consolidate"
            const result = await invokeWithNativeCompactBypass([
              "checkpoint", "decide", "--decision", "yes", "--question-request-id", decision.questionRequestID,
              "--approval-grant", decision.grant,
            ], context.sessionID, intent)
            await refreshContextCacheWithDiagnostic(result, "approval")
            if (pendingCompaction?.sessionID === context.sessionID && pendingCompactionRetiredByPython(result)) {
              pendingCompaction = undefined
              if (!persistPendingCompaction(undefined)) throw new Error("pending_compaction could not be retired")
            }
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
            const status = await invoke(["checkpoint", "status"], context.sessionID)
            const checkpoint = status.checkpoint as Json | undefined
            const intent: CheckpointIntent = checkpoint?.intent === "compact" ? "compact" : "consolidate"
            const result = await invokeWithNativeCompactBypass([
              "checkpoint", "decide", "--decision", "no", `--reason=${decision.reason}`,
              "--question-request-id", decision.questionRequestID, "--approval-grant", decision.grant,
            ], context.sessionID, intent)
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
          let status: Json
          if (pendingCompaction?.sessionID === context.sessionID) {
            // The Plugin marker can outlive the Python process. Import it
            // before status/retry so CHECKPOINT_ACTIVE cannot strand the
            // request in this file only.
            const imported = await invoke([
              "checkpoint", "request", "--trigger", "auto", "--intent", "compact",
              "--origin", "harness_auto_compaction", ...pendingCompactionArgs(pendingCompaction),
            ], context.sessionID)
            await refreshContextCacheWithDiagnostic(imported, "pending-compaction-import")
            rememberCheckpoint(imported, context.sessionID)
            if (pendingCompactionRetiredByPython(imported)) {
              pendingCompaction = undefined
              if (!persistPendingCompaction(undefined)) throw new Error("pending_compaction could not be retired")
              return JSON.stringify(imported)
            }
            const importedCheckpoint = imported.checkpoint as Json | undefined
            if (imported.approval_required === true
              || importedCheckpoint?.context_publication_status !== "completed") {
              return JSON.stringify(imported)
            }
            status = imported
          } else {
            status = await invoke(["checkpoint", "status"], context.sessionID)
          }
          const checkpoint = status.checkpoint as Json | undefined
          const operation = checkpoint?.status === "COMMITTED_WITH_PENDING_DERIVATIONS"
            ? "retry-derivations"
            : "retry"
          const retryIntent: CheckpointIntent = checkpoint?.intent === "compact"
            && checkpoint?.compaction_status !== "completed"
            ? "compact"
            : "consolidate"
          const result = await invokeWithNativeCompactBypass(["checkpoint", operation], context.sessionID, retryIntent)
          await refreshContextCacheWithDiagnostic(result, "retry")
          rememberCheckpoint(result, context.sessionID)
          if (pendingCompaction?.sessionID === context.sessionID && pendingCompactionRetiredByPython(result)) {
            pendingCompaction = undefined
            if (!persistPendingCompaction(undefined)) throw new Error("pending_compaction could not be retired")
          }
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
      if (input.tool === "pco_checkpoint") {
        if (!mainSession(input.sessionID)) throw new Error("PCO checkpoint 只能由主 session 执行。")
        if (hasAutoProvenance) {
          const provenance = await bindForegroundAutoMarkerToToolCall(input.sessionID, input.callID)
          if (foregroundAutoProvenanceUnavailable && provenance !== "manual") {
            await client.app.log({ body: {
              service: "pco",
              level: "error",
              message: "自动 checkpoint provenance 无法持久化；已拒绝执行。",
              extra: { sessionID: input.sessionID, callID: input.callID, provenance },
            } })
            throw new Error("自动 checkpoint provenance 无法持久化；拒绝执行。")
          }
          if (provenance === "mismatch" || provenance === "expired" || provenance === "consumed") {
            throw new Error("自动 checkpoint 出现重复或不匹配的工具调用；拒绝执行。")
          }
          if (provenance === "unavailable" || provenance === "unrelated") {
            // An unresolved auto marker is evidence of a control turn, not a
            // reason to reinterpret the call as manual.
            throw new Error("自动 checkpoint 的工具 provenance 不可用；拒绝降级为 manual。")
          }
          if (provenance === "manual") {
            manualCheckpointSessionID = input.sessionID
            manualCheckpointCallID = input.callID
            manualCheckpointIntent = manualCheckpointIntent ?? manualControlIntent
          }
        } else if (!await bindManualCheckpointToToolCall(input.sessionID, input.callID)) {
          throw new Error("pco_checkpoint 缺少合法 command provenance；普通 Agent 调用已拒绝。")
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
      const commandIntent = checkpointCommands.get(input.command)
      if (commandIntent && !scheduledAutoDispatch && input.arguments.trim() !== "") {
        throw new Error("/consolidate 与 /compact 不接受模型提供的 trigger/intent 参数。")
      }
      if (commandIntent || recoveryCommands.has(input.command)) {
        const marker = foregroundAutoMarker
        if (marker?.sessionID === input.sessionID) {
          if (recoveryCommands.has(input.command)) {
            // Explicit recovery commands cancel any delayed auto control turn
            // before they can mutate the checkpoint again.
            retireForegroundAutoMarker(marker.nonce, "expired")
          } else {
            const autoPart = Boolean(scheduledAutoDispatch)
              && scheduledAutoDispatch === foregroundAutoDispatch
              && scheduledAutoDispatch.intent === commandIntent
            if (autoPart) {
              scheduledAutoDispatch.commandObserved = true
              for (const part of output.parts) {
                if (part.type !== "text") continue
                const jsonPart = part as unknown as Json
                jsonPart.metadata = {
                  ...((jsonPart.metadata as Json | undefined) ?? {}),
                  pco_auto_control: true,
                  pco_intent: scheduledAutoDispatch.intent,
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
      }
      if (commandIntent) {
        if (scheduledAutoDispatch) {
          if (scheduledAutoDispatch.intent !== commandIntent) {
            throw new Error("自动 checkpoint intent 与 Host command 不匹配；拒绝执行。")
          }
        } else {
          manualCommandPendingSessionID = input.sessionID
          manualCommandPendingIntent = commandIntent
          manualControlMessageID = undefined
          manualControlIntent = commandIntent
          manualCheckpointSessionID = undefined
          manualCheckpointCallID = undefined
          manualCheckpointMessageID = undefined
          manualCheckpointIntent = undefined
        }
      }
      for (const part of output.parts) {
        if (part.type !== "text") continue
        const jsonPart = part as unknown as Json
        jsonPart.metadata = {
          ...((jsonPart.metadata as Json | undefined) ?? {}),
          pco_control: true,
          ...(commandIntent ? { pco_intent: commandIntent } : {}),
        }
      }
    },

    event: async ({ event }) => {
      await drainReceiptInbox()
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
        if (idleTask || foregroundAutoMarker || (pendingCompaction && pendingCompaction.sessionID !== event.properties.sessionID)) return
        if (existsSync(lockPath) && !pendingCompaction) return
        const task = (async () => {
          try {
            await invoke(["sync"], event.properties.sessionID)
            if (pendingCompaction?.sessionID === event.properties.sessionID) {
              const imported = await invoke([
                "checkpoint", "request", "--trigger", "auto", "--intent", "compact",
                "--origin", "harness_auto_compaction", ...pendingCompactionArgs(pendingCompaction),
              ], event.properties.sessionID)
              await refreshContextCacheWithDiagnostic(imported, "pending-compaction-idle-import")
              rememberCheckpoint(imported, event.properties.sessionID)
              if (pendingCompactionRetiredByPython(imported)) {
                pendingCompaction = undefined
                if (!persistPendingCompaction(undefined)) throw new Error("pending_compaction could not be retired")
                return
              }
              const importedCheckpoint = imported.checkpoint as Json | undefined
              if (imported.approval_required === true
                || importedCheckpoint?.context_publication_status !== "completed") return
              const resumed = await invokeWithNativeCompactBypass(
                ["checkpoint", "retry"],
                event.properties.sessionID,
                "compact",
              )
              await refreshContextCacheWithDiagnostic(resumed, "pending-harness-compaction")
              rememberCheckpoint(resumed, event.properties.sessionID)
              if (pendingCompactionRetiredByPython(resumed)) {
                pendingCompaction = undefined
                if (!persistPendingCompaction(undefined)) throw new Error("pending_compaction could not be retired")
              }
              return
            }
            const probe = await invoke(["checkpoint", "auto-probe"], event.properties.sessionID)
            const intent = autoIntentFromProbe(probe)
            // Compatibility marker: issueForegroundAutoMarker(event.properties.sessionID)
            if (intent) {
              const marker = issueForegroundAutoMarker(event.properties.sessionID, intent)
              try {
                await scheduleForegroundCheckpoint(event.properties.sessionID, marker)
              } catch (error) {
                retireForegroundAutoMarker(marker.nonce, "expired")
                await client.app.log({ body: {
                  service: "pco",
                  level: "warn",
                  message: "PCO 自动 checkpoint 无法调度前台 control turn；会话保持可输入，请执行对应命令。",
                  extra: { error: String(error), intent },
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
      const autoIntentPart = autoControlPart
        ? (autoControlPart as unknown as Json).metadata as Json | undefined
        : undefined
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
        && autoIntentPart?.pco_intent === marker.intent
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
        const manualPart = output.parts.find((part) => {
          if (part.type !== "text") return false
          const metadata = (part as unknown as Json).metadata as Json | undefined
          return metadata?.pco_control === true && metadata.pco_intent === manualCommandPendingIntent
        })
        if (!manualPart) throw new Error("manual checkpoint command intent provenance is missing")
        manualControlMessageID = stringField(output.message, "id", "messageID", "messageId")
        manualControlIntent = manualCommandPendingIntent
        manualCommandPendingSessionID = undefined
        manualCommandPendingIntent = undefined
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

- /consolidate and /compact must each call pco_checkpoint exactly once; never choose trigger or intent in tool arguments or invoke OpenCode's native compact directly.
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
      if (consumeNativeCompactBypass(input)) {
        const gate = output as unknown as Json
        gate.pco_compaction_gate = {
          decision: "allow_once",
          reason: "matching_pco_bypass_token_consumed",
        }
        return
      }

      const eventID = fieldFromInput(input, "eventID", "eventId", "requestID", "requestId", "compactionID", "compactionId")
      const requestedBoundary = fieldFromInput(input, "boundary", "contextBoundary", "messageID", "messageId")
      const requestID = eventID ?? `harness-${randomBytes(16).toString("hex")}`
      if (!pendingCompaction) {
        pendingCompaction = {
          requestID,
          eventID,
          sessionID: input.sessionID,
          requestedBoundary,
          requestedAt: Date.now(),
          origin: "harness_auto_compaction",
        }
        if (!persistPendingCompaction(pendingCompaction)) {
          throw new Error("Harness compaction intercepted but durable pending_compaction could not be written")
        }
      } else if (pendingCompaction.sessionID !== input.sessionID) {
        throw new Error("A different session already owns the pending Harness compaction")
      }

      const gate = output as unknown as Json
      gate.cancel = true
      gate.preventDefault = true
      gate.blocked = true
      gate.pco_compaction_gate = {
        decision: "intercept",
        request_id: pendingCompaction.requestID,
        trigger: "auto",
        intent: "compact",
        origin: "harness_auto_compaction",
      }
      try {
        const result = await invokeWithNativeCompactBypass([
          "checkpoint", "request", "--trigger", "auto", "--intent", "compact", "--origin", "harness_auto_compaction",
          ...pendingCompactionArgs(pendingCompaction),
        ], input.sessionID, "compact")
        await refreshContextCacheWithDiagnostic(result, "harness-auto-compaction")
        rememberCheckpoint(result, input.sessionID)
        if (pendingCompactionRetiredByPython(result)) {
          pendingCompaction = undefined
          if (!persistPendingCompaction(undefined)) throw new Error("pending_compaction could not be retired")
        }
      } catch (error) {
        await client.app.log({ body: {
          service: "pco",
          level: "error",
          message: "Harness compaction was intercepted; PCO compact recovery is pending.",
          extra: { requestID, sessionID: input.sessionID, error: String(error) },
        } })
        // Keep the durable request and input lock. The current Harness event
        // is still blocked below; pco-retry must resume the durable boundary.
      }
      // The 1.17 SDK has no typed cancellation field. Throwing after the
      // structured gate is the synchronous fail-closed fallback; a Host with
      // cancellation support may consume `cancel=true` and avoid the error.
      throw new Error("Harness native compaction intercepted by PCO; consolidate and publish context first")
    },

    "experimental.compaction.autocontinue": async (input, output) => {
      if (mainSession(input.sessionID)) output.enabled = false
    },
  }
}
