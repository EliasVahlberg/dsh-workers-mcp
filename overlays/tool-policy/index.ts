/**
 * tool-policy: per-permission-preset allow / deny / ask rules for tool calls.
 *
 * A dsh permission preset bundles two knobs, sandbox mode and approval policy.
 * Alone, that only governs *sandbox escalation* — no stock tool asks for
 * approval otherwise. This plugin adds the missing layer: before every tool
 * call it looks up the calling session's effective preset and applies that
 * preset's first matching rule.
 *
 *   allow → the call runs
 *   deny  → the model immediately gets an error carrying the rule's reason;
 *           nobody is asked
 *   ask   → dsh's approval service decides. Under an `ask` approval policy the
 *           request reaches an answerer (a UI, or an orchestrator via
 *           /api/respond). Under `never` the service rejects it itself, before
 *           any answerer runs — so one rule list can mean "ask the
 *           orchestrator" in one preset and "reject outright" in another.
 *
 * What this is NOT: a security boundary. Matching a bash command string is
 * trivially evaded (`sh -c`, base64, a script file), and `curl` from bash
 * reaches the network regardless. Rules here define *scope* — they stop an
 * agent doing something by mistake or out of task. Confinement against a
 * hijacked agent is the job of an OS sandbox around the whole dsh process.
 *
 * Fail-closed: a session whose knobs match no table entry (dsh reports that as
 * the derived `custom` preset), an unknown preset name, or a deployment without
 * the permission-preset service all get the `fallback` rule set rather than
 * silently allowing everything.
 *
 * Subagents: dsh's in-process delegation copies the parent's sandbox override
 * and pins the child's approval policy to `never`
 * (subagent/src/child-agent.ts, captureDelegatedPolicyOverrides). So a child of
 * a `supervised` worker resolves to `autonomous` here, and a child of
 * `read-only` resolves to `custom` → `fallback`. That is the intended shape: a
 * child can never be asked about anything, so it gets the reject-outright rules.
 *
 * Deliberately dependency-free at runtime: every import is type-only (erased
 * by the loader's TypeScript transform), and the Config validator is a
 * hand-written Standard Schema object rather than Schemastery. The plugin is
 * loaded by absolute path from outside the dsh workspace, where
 * `@deepseek-ai/*` runtime packages would not resolve.
 */

import type { Context } from '@deepseek-ai/cordis'

/** One rule. The first rule whose tool and argument pattern match wins. */
export interface Rule {
  /** Exact tool name, or `*` for any tool. */
  tool: string
  /**
   * Argument to test, by top-level field name (e.g. `command` for bash, `url`
   * for web_fetch). An array value is joined with newlines before matching.
   * Omit to match on the tool name alone.
   */
  argument?: string
  /** JavaScript regular expression tested against the argument. Required with `argument`. */
  pattern?: string
  decision: 'allow' | 'deny' | 'ask'
  /** Shown to the model on deny, and to the approver on ask. */
  reason?: string
}

export interface PresetPolicy {
  rules: Rule[]
  /** Decision when no rule matches. */
  default: 'allow' | 'deny' | 'ask'
}

export interface Config {
  /** Preset name (as in the permission-preset table) → its policy. */
  presets: Record<string, PresetPolicy>
  /** Applied to `custom`, unknown presets, and sessions with no preset service. */
  fallback: PresetPolicy
}

const DECISIONS = new Set(['allow', 'deny', 'ask'])

type Issue = { message: string; path?: ReadonlyArray<PropertyKey> }

function checkPolicy(value: unknown, path: PropertyKey[], issues: Issue[]): void {
  if (typeof value !== 'object' || value === null) {
    issues.push({ message: 'expected a policy object { rules, default }', path })
    return
  }
  const policy = value as Record<string, unknown>
  if (!DECISIONS.has(policy.default as string)) {
    issues.push({ message: 'default must be allow | deny | ask', path: [...path, 'default'] })
  }
  if (!Array.isArray(policy.rules)) {
    issues.push({ message: 'rules must be an array', path: [...path, 'rules'] })
    return
  }
  policy.rules.forEach((raw, index) => {
    const at = [...path, 'rules', index]
    if (typeof raw !== 'object' || raw === null) {
      issues.push({ message: 'expected a rule object', path: at })
      return
    }
    const rule = raw as Record<string, unknown>
    if (typeof rule.tool !== 'string' || rule.tool === '') {
      issues.push({ message: 'tool must be a non-empty string (a tool name, or *)', path: [...at, 'tool'] })
    }
    if (!DECISIONS.has(rule.decision as string)) {
      issues.push({ message: 'decision must be allow | deny | ask', path: [...at, 'decision'] })
    }
    if ((rule.argument === undefined) !== (rule.pattern === undefined)) {
      issues.push({ message: 'argument and pattern must be given together', path: at })
    }
    if (typeof rule.pattern === 'string') {
      // Compile at load so a typo'd regex fails the boot, not the first tool call.
      try { new RegExp(rule.pattern) } catch (error) {
        issues.push({ message: `invalid pattern: ${String(error)}`, path: [...at, 'pattern'] })
      }
    }
  })
}

/**
 * Standard Schema v1 validator, hand-written (see the module note on why not
 * Schemastery). Cordis calls `~standard.validate` synchronously before apply.
 */
export const Config = {
  '~standard': {
    version: 1 as const,
    vendor: 'tool-policy',
    validate(value: unknown) {
      const issues: Issue[] = []
      if (typeof value !== 'object' || value === null) {
        return { issues: [{ message: 'tool-policy: config must be an object { presets, fallback }' }] }
      }
      const config = value as Record<string, unknown>
      if (typeof config.presets !== 'object' || config.presets === null) {
        issues.push({ message: 'presets must be an object of preset name → policy', path: ['presets'] })
      } else {
        for (const [name, policy] of Object.entries(config.presets)) {
          checkPolicy(policy, ['presets', name], issues)
        }
      }
      checkPolicy(config.fallback, ['fallback'], issues)
      return issues.length > 0 ? { issues } : { value: value as Config }
    },
  },
}

export const name = 'tool-policy'

interface CompiledRule extends Rule { regex?: RegExp }
interface CompiledPolicy { rules: CompiledRule[]; default: PresetPolicy['default'] }

function compile(policy: PresetPolicy): CompiledPolicy {
  return {
    default: policy.default,
    rules: policy.rules.map(rule => ({
      ...rule,
      ...(rule.pattern === undefined ? {} : { regex: new RegExp(rule.pattern) }),
    })),
  }
}

/** Stringify one argument for matching. Missing → undefined, so the rule does not match. */
function argumentText(args: unknown, field: string): string | undefined {
  if (typeof args !== 'object' || args === null) return undefined
  const value = (args as Record<string, unknown>)[field]
  if (value === undefined || value === null) return undefined
  if (Array.isArray(value)) return value.map(String).join('\n')
  return typeof value === 'string' ? value : JSON.stringify(value)
}

/** First matching rule, else the policy default. Exported for tests. */
export function decide(
  policy: CompiledPolicy,
  toolName: string,
  args: unknown,
): { decision: Rule['decision']; reason?: string; matched: number } {
  for (const [index, rule] of policy.rules.entries()) {
    if (rule.tool !== '*' && rule.tool !== toolName) continue
    if (rule.regex !== undefined) {
      const text = argumentText(args, rule.argument as string)
      if (text === undefined || !rule.regex.test(text)) continue
    }
    return { decision: rule.decision, ...(rule.reason === undefined ? {} : { reason: rule.reason }), matched: index }
  }
  return { decision: policy.default, matched: -1 }
}

/** Minimal structural view of the permission-preset service we read. */
interface PresetReader { current(events: readonly unknown[]): string }

export function apply(ctx: Context, config: Config): void {
  const presets = new Map(Object.entries(config.presets).map(([k, v]) => [k, compile(v)]))
  const fallback = compile(config.fallback)
  const logger = (ctx as unknown as { logger?: (n: string) => { info(...a: unknown[]): void } })
    .logger?.('tool-policy')

  const policyFor = (exec: { agent?: { session: { events: readonly unknown[] } } }): { name: string; policy: CompiledPolicy } => {
    const service = (ctx as unknown as { get(k: string): unknown }).get('permissionPresets') as PresetReader | undefined
    if (service === undefined || exec.agent === undefined) return { name: '(no preset service)', policy: fallback }
    const current = service.current(exec.agent.session.events)
    const policy = presets.get(current)
    return policy === undefined ? { name: current, policy: fallback } : { name: current, policy }
  }

  // The event is declared by dsh-tools, whose module augmentation this
  // out-of-tree file cannot see; hence the structural casts.
  const on = (ctx as unknown as {
    on(event: string, listener: (exec: any, next: () => Promise<unknown>) => Promise<unknown>): void
  }).on.bind(ctx)

  on('tools/pre-execute', async (exec, next) => {
    const { name: preset, policy } = policyFor(exec)
    const verdict = decide(policy, exec.name, exec.arguments)
    if (verdict.decision === 'allow') return next()

    const reason = `${verdict.reason ?? `tool "${exec.name}" is not permitted`} [tool-policy: preset ${preset}]`
    logger?.info('%s %s under %s (rule %d)', verdict.decision, exec.name, preset, verdict.matched)
    return verdict.decision === 'deny'
      ? { kind: 'deny', reason }
      : { kind: 'ask', reason }
  })
}
