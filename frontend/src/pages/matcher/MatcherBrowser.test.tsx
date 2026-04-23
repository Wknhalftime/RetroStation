// @vitest-environment jsdom
/**
 * MatcherBrowser component tests.
 *
 * These tests assert the observable contract of the queue browser via
 * mocked `apiFetch`. They deliberately scope to rendering + a single
 * user interaction per test to avoid flakey multi-page React Query
 * timing races — deeper flows (pagination state across multiple
 * fetches, clamp effects after total shrinks) are covered implicitly
 * by the hook-level tests and the comments describing the invariants
 * in MatcherBrowser.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { QueueArtist } from '@/lib/schemas/matcher'

vi.mock('@/api/client', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from '@/api/client'
import { MatcherBrowser } from './MatcherBrowser'

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
}

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

const mockedApiFetch = vi.mocked(apiFetch)

beforeEach(() => {
  mockedApiFetch.mockReset()
})

function makeArtist(id: string, name: string): QueueArtist {
  return {
    id,
    original_name: name,
    normalized_name: name.toLowerCase(),
    match_status: 'needs_review',
    triage_bucket: 'blocked',
    candidates: [],
    identities: [],
  }
}

describe('MatcherBrowser rendering', () => {
  it('shows "Resolution Center" heading and the queue count', async () => {
    mockedApiFetch.mockResolvedValue({
      items: [makeArtist('00000000-0000-0000-0000-00000000000a', 'Alpha')],
      total: 1,
    })
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) })
    expect(screen.getByRole('heading', { name: /Resolution Center/i })).toBeDefined()
    await waitFor(() => {
      expect(screen.getByText(/Artists \(1\)/)).toBeDefined()
    })
  })

  it('shows "Queue is empty" only when the backend reports total === 0', async () => {
    // Regression: before the fix in PR #25 round 3, an empty page response
    // with total > 0 wrongly triggered the "queue empty" UI. The condition
    // is now gated on `total === 0`.
    mockedApiFetch.mockResolvedValue({ items: [], total: 0 })
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) })
    await waitFor(() => {
      expect(screen.getByText(/Queue is empty/i)).toBeDefined()
    })
  })

  it('does NOT show "Queue is empty" when total > 0 even if items is empty (off-range page simulation)', async () => {
    mockedApiFetch.mockResolvedValue({ items: [], total: 10 })
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) })
    // Give React Query a tick to settle.
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByText(/Queue is empty/i)).toBeNull()
  })
})

describe('MatcherBrowser — MB search target-artist capture', () => {
  it('captures the queue artist ID at MB-search-open time so a later selection change does not redirect the mutation', async () => {
    const artistA = makeArtist(
      '00000000-0000-0000-0000-00000000000a',
      'Alpha',
    )
    const artistB = makeArtist(
      '00000000-0000-0000-0000-00000000000b',
      'Beta',
    )
    mockedApiFetch.mockImplementation(async (url) => {
      const u = typeof url === 'string' ? url : ''
      if (u.includes('/api/v1/matching/queue')) {
        return { items: [artistA, artistB], total: 2 }
      }
      if (u.includes('/api/v1/matching/mb-artists')) {
        return {
          items: [
            {
              id: 'mbid-target',
              name: 'Target',
              score: 100,
              disambiguation: '',
            },
          ],
        }
      }
      if (u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/)) {
        return { id: 'x', match_status: 'manual_matched' }
      }
      return {}
    })
    render(<MatcherBrowser />, { wrapper: wrapperFor(makeClient()) })

    // Wait for the sidebar list to render, then scope interactions to it.
    // The sidebar buttons contain the artist name PLUS a status badge, so
    // we match by substring rather than exact name.
    await screen.findByText(/Artists \(2\)/)
    const sidebar = screen.getByText(/Artists \(2\)/).closest('aside') as HTMLElement
    const alphaSidebarButton = within(sidebar).getByRole('button', { name: /Alpha/i })
    const betaSidebarButton = within(sidebar).getByRole('button', { name: /Beta/i })

    fireEvent.click(alphaSidebarButton)
    fireEvent.click(
      await screen.findByRole('button', { name: /Search MusicBrainz/i }),
    )

    // Change queue selection to Beta WHILE the slide-over is open.
    fireEvent.click(betaSidebarButton)

    // The slide-over's searchbox — scope to its aria-labelled aside so we
    // don't pick up the file-mode slide-over also present in the DOM.
    const mbAside = screen.getByRole('complementary', {
      name: /Search MusicBrainz artists/i,
    })
    const searchInput = mbAside.querySelector(
      'input[type="search"]',
    ) as HTMLInputElement
    fireEvent.change(searchInput, { target: { value: 'target' } })

    // Click the MB result.
    const mbResultButton = await screen.findByRole('button', {
      name: /Target/i,
    })
    fireEvent.click(mbResultButton)

    // The resolve mutation must hit Alpha's URL, NOT Beta's.
    await waitFor(() => {
      const resolveCalls = mockedApiFetch.mock.calls.filter(
        ([u]) =>
          typeof u === 'string' &&
          !!u.match(/\/api\/v1\/matching\/artists\/[^/]+\/resolve/),
      )
      expect(resolveCalls.length).toBeGreaterThan(0)
      const url = resolveCalls[0][0] as string
      expect(url).toContain(artistA.id)
      expect(url).not.toContain(artistB.id)
    })
  })
})
