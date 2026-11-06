import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { clearClarifyRequest, setClarifyRequest } from '@/store/clarify'
import { resetServerRequestsForTests } from '@/store/server-requests'
import { $activeSessionId } from '@/store/session'

import { ClarifyTool, resetClarifyCardBindingsForTests, suppressedClarifyRowCount } from './index'

// One session keeps ONE open clarify request, and the pending card renders that request's
// questions. Every unsettled clarify row in the transcript therefore used to paint the same
// question — one decision appeared twice whenever a second row was left open (two calls in one
// turn, a lost tool.complete, a sparse hydration). Only the row that owns the request may paint
// it; the rest render as plain history.

vi.mock('@assistant-ui/react', () => ({
  useAuiState: () => true
}))

// jsdom has no Element.animate.
vi.mock('@/lib/use-enter-animation', () => ({ useEnterAnimation: () => undefined }))

const SESSION = 'session-dup'

beforeEach(() => {
  resetClarifyCardBindingsForTests()
  $activeSessionId.set(SESSION)
})

afterEach(() => {
  cleanup()
  clearClarifyRequest()
  resetServerRequestsForTests()
  $activeSessionId.set(null)
  vi.clearAllMocks()
})

function tree(ui: ReactNode) {
  return (
    <I18nProvider configClient={null} initialLocale="en">
      {ui}
    </I18nProvider>
  )
}

function row(toolCallId: string, question: string): ToolCallMessagePartProps {
  const args = { questions: [{ choices: ['a', 'b'], question }] }

  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result: undefined,
    resume: vi.fn(),
    status: { type: 'running' },
    toolCallId,
    toolName: 'clarify',
    type: 'tool-call'
  } as ToolCallMessagePartProps
}

function parkRequest(question: string) {
  setClarifyRequest({
    questions: [{ choices: ['a', 'b'], multiSelect: false, qid: 'q0', question }],
    requestId: 'request-dup',
    sessionId: SESSION
  })
}

describe('one live clarify card per request', () => {
  it('paints the live question once when an older unsettled row is still in the transcript', () => {
    parkRequest('Which path?')

    render(
      tree(
        <>
          <ClarifyTool {...row('call-old', 'Which deployment target?')} />
          <ClarifyTool {...row('call-live', 'Which path?')} />
        </>
      )
    )

    expect(screen.getAllByText('Which path?')).toHaveLength(1)
    expect(suppressedClarifyRowCount(SESSION, 'request-dup')).toBe(1)
  })

  it('still paints the card when the owning row comes first', () => {
    parkRequest('Which path?')

    render(
      tree(
        <>
          <ClarifyTool {...row('call-live', 'Which path?')} />
          <ClarifyTool {...row('call-old', 'Which deployment target?')} />
        </>
      )
    )

    expect(screen.getAllByText('Which path?')).toHaveLength(1)
    expect(suppressedClarifyRowCount(SESSION, 'request-dup')).toBe(1)
  })

  it('paints the live question once for a single row, and never counts it as suppressed', () => {
    parkRequest('Which path?')

    render(tree(<ClarifyTool {...row('call-live', 'Which path?')} />))

    expect(screen.getAllByText('Which path?')).toHaveLength(1)
    expect(suppressedClarifyRowCount(SESSION, 'request-dup')).toBe(0)
  })
})
