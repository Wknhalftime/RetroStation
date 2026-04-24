// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('@/api/client', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from '@/api/client'
import { SearchSlideOver } from './SearchSlideOver'

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

describe('SearchSlideOver mb-artist mode', () => {
  it('does not fetch when slide-over is closed', () => {
    // The component debounces input at 300ms and React Query schedules
    // fetches on enabled-key change. Fake timers advance past both so a
    // missing `open` gate would deterministically produce a fetch — if
    // no fetch happens here after the timer advance, the gate is solid.
    vi.useFakeTimers()
    try {
      render(
        <SearchSlideOver open={false} onClose={vi.fn()} mode="mb-artist" />,
        { wrapper: wrapperFor(makeClient()) },
      )
      vi.advanceTimersByTime(500)
      expect(mockedApiFetch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('routes search through the mb-artists endpoint when open', async () => {
    mockedApiFetch.mockResolvedValue({
      items: [
        {
          id: 'mbid-prince-xxx',
          name: 'Prince',
          score: 100,
          disambiguation: '',
        },
      ],
    })
    render(
      <SearchSlideOver open={true} onClose={vi.fn()} mode="mb-artist" />,
      { wrapper: wrapperFor(makeClient()) },
    )
    const input = screen.getByRole('searchbox')
    fireEvent.change(input, { target: { value: 'prince' } })
    await waitFor(() => {
      expect(mockedApiFetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/matching/mb-artists?query=prince'),
      )
    })
    // The library-artists endpoint must NOT be hit in mb-artist mode.
    expect(mockedApiFetch).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/library/artists'),
    )
  })

  it('invokes onSelectMbArtist with the MBID on result click', async () => {
    mockedApiFetch.mockResolvedValue({
      items: [
        {
          id: 'mbid-prince-xxx',
          name: 'Prince',
          score: 100,
          disambiguation: 'The Artist',
        },
      ],
    })
    const onSelectMbArtist = vi.fn()
    const onClose = vi.fn()
    render(
      <SearchSlideOver
        open={true}
        onClose={onClose}
        mode="mb-artist"
        onSelectMbArtist={onSelectMbArtist}
      />,
      { wrapper: wrapperFor(makeClient()) },
    )
    const input = screen.getByRole('searchbox')
    fireEvent.change(input, { target: { value: 'prince' } })
    const resultButton = await screen.findByRole('button', {
      name: /Prince/i,
    })
    fireEvent.click(resultButton)
    expect(onSelectMbArtist).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'mbid-prince-xxx', name: 'Prince' }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('gates the "No results found" empty-state on trimmed query', async () => {
    mockedApiFetch.mockResolvedValue({ items: [] })
    // Fake timers so we can deterministically advance past the 300ms
    // input debounce and assert no empty state (and no fetch) ever
    // fires for whitespace-only input.
    vi.useFakeTimers()
    try {
      render(
        <SearchSlideOver open={true} onClose={vi.fn()} mode="mb-artist" />,
        { wrapper: wrapperFor(makeClient()) },
      )
      const input = screen.getByRole('searchbox')
      fireEvent.change(input, { target: { value: '   ' } })
      await vi.advanceTimersByTimeAsync(500)
      expect(screen.queryByText('No results found.')).toBeNull()
      expect(mockedApiFetch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('closes on Escape key', () => {
    const onClose = vi.fn()
    render(
      <SearchSlideOver open={true} onClose={onClose} mode="mb-artist" />,
      { wrapper: wrapperFor(makeClient()) },
    )
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
