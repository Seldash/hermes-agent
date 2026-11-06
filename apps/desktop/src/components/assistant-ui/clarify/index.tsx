'use client'

import { type ToolCallMessagePartProps, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { normalize } from '@/lib/text'
import { $settledClarifyResults, type ClarifyRequest, sessionClarifyRequest } from '@/store/clarify'

import { selectMessageRunning } from '../tool/fallback-model'

import { readClarifyArgs } from './parse'
import { ClarifyToolPending } from './pending'
import { ClarifyToolSettled } from './settled'
import { useUndeliveredClarify } from './use-undelivered'

export const ClarifyTool = (props: ToolCallMessagePartProps) => {
  // Answered → settled Q&A (ToolFallback collapsed the answer away).
  if (props.result !== undefined) {
    return <ClarifyToolSettled {...props} />
  }

  return <ClarifyToolLive {...props} />
}

// The request each tool row asked, remembered past its clearing and past a
// remount (a session switch, a stopped turn): a skip or a typed answer settles
// it in the store before (or without) `tool.complete`. Keyed by session,
// message and call: some providers name every call "call_0", and a bare call id
// would show a later card the answer of an earlier one.
const requestIdByToolCall = new Map<string, string>()
// Which row owns an OPEN request. The store keeps one clarify per session, and
// ClarifyToolPending renders `request.questions` whenever a request is live — so
// without an owner every unsettled clarify row in the transcript paints the same
// question. A turn that raised two calls (or a row left unsettled by a lost
// `tool.complete`) then shows one question twice.
const rowKeyByRequestId = new Map<string, string>()
// Suppressed duplicates per request, for recurrence counting: "did this chat show
// two cards" is otherwise indistinguishable from the user reopening the same chat.
const suppressedRowsByRequest = new Map<string, Set<string>>()

function questionTexts(values: { question?: string }[] | undefined): string[] {
  return (values ?? []).map(entry => normalize(entry.question)).filter(Boolean)
}

/** Does this row's own args name the live request's questions? A row hydrated without args
 *  cannot match; it is handled by the first-claim rule in {@link ClarifyToolLive}. */
function argsMatchQuestions(fromArgs: { questions?: { question?: string }[] }, request: ClarifyRequest | null): boolean {
  const argsQuestions = questionTexts(fromArgs.questions)
  const liveQuestions = questionTexts(request?.questions)

  return argsQuestions.length > 0 && argsQuestions.join('\u0000') === liveQuestions.join('\u0000')
}

function noteSuppressedCard(sessionId: string | null, requestId: string, rowKey: string): void {
  const key = `${sessionId ?? ''}:${requestId}`
  const rows = suppressedRowsByRequest.get(key) ?? new Set<string>()

  if (rows.has(rowKey)) {
    return
  }

  rows.add(rowKey)
  suppressedRowsByRequest.set(key, rows)

  if (rows.size > 1) {
    console.warn('[clarify] duplicate live card suppressed', { session: sessionId, request: requestId })
  }
}

/** Rows that would have painted a live request they do not own (test/telemetry hook). */
export function suppressedClarifyRowCount(sessionId: string | null | undefined, requestId: string): number {
  return suppressedRowsByRequest.get(`${sessionId ?? ''}:${requestId}`)?.size ?? 0
}

/** Drop every row↔request binding. Bindings are keyed by live ids, so this is test isolation only. */
export function resetClarifyCardBindingsForTests(): void {
  requestIdByToolCall.clear()
  rowKeyByRequestId.clear()
  suppressedRowsByRequest.clear()
}

function ClarifyToolLive(props: ToolCallMessagePartProps) {
  // The tool row is in whichever session's transcript rendered it — read THAT
  // session's clarify (primary or tile), not the globally-active one.
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionClarifyRequest(sessionId), [sessionId])
  const request = useStore($request)
  const fromArgs = useMemo(() => readClarifyArgs(props.args), [props.args])
  const messageRunning = useAuiState(selectMessageRunning)
  const messageId = useAuiState(s => s.message.id)
  const rowKey = `${sessionId ?? ''}:${messageId}:${props.toolCallId}`
  // Answering clears the request a beat before `tool.complete` swaps in the
  // settled card. Latch submit so that gap doesn't demote; Stop also clears
  // the request and must still collapse an unanswered card.
  const [answered, setAnswered] = useState(false)
  const settledResults = useStore($settledClarifyResults)
  const requestId = requestIdByToolCall.get(rowKey)
  const settledResult = requestId && request?.requestId !== requestId ? settledResults[requestId] : undefined
  // Ownership decides who paints the live card: the row whose args name these questions, else
  // the first unsettled row that has no args to match on (a sparse hydration). A row that
  // already names a DIFFERENT question must never paint this request — that is the duplicate.
  const argsQuestions = questionTexts(fromArgs.questions)
  const claimedRow = request ? rowKeyByRequestId.get(request.requestId) : undefined
  const argsMatch = Boolean(request) && argsMatchQuestions(fromArgs, request)

  const ownsRequest =
    Boolean(request) && (argsMatch || claimedRow === rowKey || (claimedRow === undefined && argsQuestions.length === 0))

  if (request && ownsRequest && !settledResult && request.requestId !== requestId) {
    requestIdByToolCall.set(rowKey, request.requestId)
    rowKeyByRequestId.set(request.requestId, rowKey)
  }

  if (request && settledResult) {
    rowKeyByRequestId.delete(request.requestId)
  }

  const undelivered = useUndeliveredClarify(sessionId, messageRunning && !request && !answered && !settledResult)

  if (settledResult) {
    return <ClarifyToolSettled {...props} result={settledResult} />
  }

  // A second unsettled row for a request another row already owns: history, not a question.
  // Rendering the card here is exactly how one question used to appear twice.
  if (request && !ownsRequest) {
    noteSuppressedCard(sessionId, request.requestId, rowKey)

    return <ToolFallback {...props} />
  }

  // Stopped mid-prompt with no result — don't leave a dead interactive panel.
  // `session.info` reports running=false while clarify is blocking, so the
  // running flag alone would remount the question as a tool row. Keep the
  // card while a request is open or this instance already submitted.
  if (!messageRunning && !request && !answered) {
    return <ToolFallback {...props} />
  }

  return (
    <ClarifyToolPending
      fromArgs={fromArgs}
      onAnswered={() => setAnswered(true)}
      request={request}
      undelivered={undelivered}
    />
  )
}
