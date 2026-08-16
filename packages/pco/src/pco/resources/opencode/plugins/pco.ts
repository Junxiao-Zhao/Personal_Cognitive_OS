import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"
import { createHmac, randomBytes } from "node:crypto"
import type { Plugin } from "@opencode-ai/plugin"
import { tool } from "@opencode-ai/plugin"

type Json = Record<string, unknown>
type PendingApproval = {
  checkpointID: string
  proposalHash: string
  challengeID: string
  sessionID: string
}

export const PCOPlugin: Plugin = async ({ client, directory, serverUrl }) => {
  const workspace = resolve(process.env.PCO_WORKSPACE ?? resolve(directory, ".pco"))
  const stateRoot = resolve(workspace, "state")
  const contextPath = resolve(stateRoot, "context", "current.md")
  const lockPath = resolve(stateRoot, "checkpoint-lock.json")
  const bindingPath = resolve(stateRoot, "harness-binding.json")
  const pcoCommand = process.env.PCO_COMMAND ?? "pco"
  let currentContext = existsSync(contextPath) ? readFileSync(contextPath, "utf8") : ""
  let idleTask: Promise<void> | undefined
  const approvalSecret = randomBytes(32).toString("hex")
  let pendingApproval: PendingApproval | undefined
  let approvalGrant: string | undefined
  const controlCommands = new Set(["compact", "pco-abort", "pco-no", "pco-retry", "pco-status", "pco-yes"])

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
      pendingApproval = undefined
      approvalGrant = undefined
      return
    }
    pendingApproval = {
      checkpointID: String(proposal.checkpoint_id),
      proposalHash: String(proposal.proposal_hash),
      challengeID: String(proposal.approval_challenge_id),
      sessionID,
    }
    approvalGrant = undefined
  }

  const mintApprovalGrant = (sessionID: string) => {
    if (!pendingApproval || pendingApproval.sessionID !== sessionID) return undefined
    const payload = {
      grant_id: randomBytes(16).toString("hex"),
      checkpoint_id: pendingApproval.checkpointID,
      proposal_hash: pendingApproval.proposalHash,
      challenge_id: pendingApproval.challengeID,
      session_id: sessionID,
      issued_at: Math.floor(Date.now() / 1000),
    }
    const encoded = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url")
    const signature = createHmac("sha256", approvalSecret).update(encoded).digest("hex")
    approvalGrant = `${encoded}.${signature}`
    return approvalGrant
  }

  const rehydrateApproval = async (sessionID: string) => {
    if (pendingApproval?.sessionID === sessionID) return
    try {
      const result = await invoke(["checkpoint", "status"], sessionID)
      const checkpoint = result.checkpoint as Json | undefined
      const proposal = result.proposal as Json | undefined
      if (checkpoint?.status === "AWAITING_META_APPROVAL" && proposal) {
        rememberCheckpoint({ approval_required: true, proposal }, sessionID)
      }
    } catch {
      // A status read is best-effort here; pco_approve still fails closed.
    }
  }

  const scheduleForegroundCheckpoint = async (sessionID: string) => {
    const session = client.session as unknown as {
      command?: (input: { path: { id: string }; body: { command: string; arguments: string } }) => Promise<unknown>
      prompt?: (input: { path: { id: string }; body: { parts: Array<{ type: string; text: string }> } }) => Promise<unknown>
    }
    if (typeof session.command === "function") {
      await session.command({ path: { id: sessionID }, body: { command: "compact", arguments: "" } })
      return
    }
    if (typeof session.prompt === "function") {
      await session.prompt({ path: { id: sessionID }, body: { parts: [{ type: "text", text: "/compact" }] } })
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

  return {
    tool: {
      pco_checkpoint: tool({
        description: "Run the PCO manual checkpoint. Call exactly once for /compact; return an approval proposal when Meta-memory is protected.",
        args: {},
        async execute(_args, context) {
          context.metadata({ title: "PCO checkpoint" })
          const result = await invoke(["checkpoint", "request", "--trigger", "manual"], context.sessionID)
          rememberCheckpoint(result, context.sessionID)
          return JSON.stringify(result)
        },
      }),
      pco_approve: tool({
        description: "Approve the exact pending PCO Meta-memory proposal.",
        args: {},
        async execute(_args, context) {
          await rehydrateApproval(context.sessionID)
          if (!approvalGrant || !pendingApproval || pendingApproval.sessionID !== context.sessionID) {
            throw new Error("请先由用户在主会话执行 /pco-yes；模型不能直接伪造 Meta 批准。")
          }
          try {
            const result = await invoke([
              "checkpoint", "decide", "--decision", "yes", "--decision-message-id", context.messageID,
              "--approval-grant", approvalGrant,
            ], context.sessionID)
            pendingApproval = undefined
            return JSON.stringify(result)
          } finally {
            // A grant is single-use. On failure, /pco-yes rehydrates the
            // durable proposal and mints a fresh grant for a retry.
            approvalGrant = undefined
          }
        },
      }),
      pco_reject: tool({
        description: "Reject the pending PCO Meta-memory proposal with the user's required reason or supplemental experience. Do not ask another follow-up after this call.",
        args: {
          reason: tool.schema.string().min(1).describe("The user's non-empty objection, self-understanding, or supplemental experience"),
        },
        async execute(args, context) {
          return JSON.stringify(await invoke([
            "checkpoint", "decide", "--decision", "no", "--reason", args.reason,
            "--decision-message-id", context.messageID,
          ], context.sessionID))
        },
      }),
      pco_status: tool({
        description: "Read the current PCO checkpoint status without changing it.",
        args: {},
        async execute(_args, context) {
          return JSON.stringify(await invoke(["checkpoint", "status"], context.sessionID))
        },
      }),
      pco_retry: tool({
        description: "Retry checkpoint recovery or any pending post-commit derivations from their durable boundary.",
        args: {},
        async execute(_args, context) {
          const status = await invoke(["checkpoint", "status"], context.sessionID)
          const checkpoint = status.checkpoint as Json | undefined
          const operation = checkpoint?.status === "COMMITTED_WITH_PENDING_DERIVATIONS"
            ? "retry-derivations"
            : "retry"
          return JSON.stringify(await invoke(["checkpoint", operation], context.sessionID))
        },
      }),
      pco_abort: tool({
        description: "Abort an uncommitted PCO checkpoint. A committed checkpoint cannot be aborted.",
        args: {},
        async execute(_args, context) {
          return JSON.stringify(await invoke(["checkpoint", "abort"], context.sessionID))
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

    "command.execute.before": async (input, output) => {
      if (!mainSession(input.sessionID) || !controlCommands.has(input.command)) return
      if (input.command === "pco-yes") {
        await rehydrateApproval(input.sessionID)
        mintApprovalGrant(input.sessionID)
      }
      for (const part of output.parts) {
        if (part.type !== "text") continue
        const jsonPart = part as unknown as Json
        jsonPart.metadata = { ...((jsonPart.metadata as Json | undefined) ?? {}), pco_control: true }
      }
    },

    event: async ({ event }) => {
      if (event.type === "file.watcher.updated") {
        const changed = resolve(directory, event.properties.file)
        if (changed === contextPath && existsSync(contextPath)) currentContext = readFileSync(contextPath, "utf8")
      }
      if (event.type === "session.idle" && mainSession(event.properties.sessionID)) {
        if (existsSync(lockPath) || idleTask) return
        const task = (async () => {
          try {
            await invoke(["sync"], event.properties.sessionID)
            const probe = await invoke(["checkpoint", "auto-probe"], event.properties.sessionID)
            if (probe.needed === true) {
              try {
                await scheduleForegroundCheckpoint(event.properties.sessionID)
              } catch (error) {
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
      throw new Error("PCO checkpoint 正在进行，普通输入已锁定。请使用 /pco-status、/pco-yes、/pco-no <理由>、/pco-retry 或 /pco-abort。")
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID || !mainSession(input.sessionID)) return
      if (existsSync(contextPath)) currentContext = readFileSync(contextPath, "utf8")
      output.system.push(`
## PCO runtime contract

You are the user's long-term PCO companion. Use the pco-memory skill for onboarding,
self-exploration, evidence boundaries, correction, and checkpoint behavior.

- /compact must call pco_checkpoint exactly once; never invoke OpenCode's native compact directly.
- When a proposal needs approval, show its exact protected Meta diff, evidence, and proposal hash, then ask the user to execute /pco-yes or /pco-no with a non-empty reason. Only /pco-yes mints the host approval grant; a model-generated pco_approve call is not authorization.
- /pco-yes calls pco_approve. /pco-no carries the user's non-empty reason in the same command and calls pco_reject; after rejection, do not ask another follow-up.
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
