import { describe, expect, it } from 'vitest'
import { firstCandidateMbid } from './candidates'
import type { QueueArtist } from '@/lib/schemas/matcher'

function artistWithCandidates(
  candidates: QueueArtist['candidates'],
): QueueArtist {
  return {
    id: '00000000-0000-0000-0000-000000000001',
    original_name: 'Prince',
    normalized_name: 'prince',
    match_status: 'needs_review',
    triage_bucket: 'blocked',
    candidates,
    identities: [],
  }
}

describe('firstCandidateMbid', () => {
  it('returns null for a null artist', () => {
    expect(firstCandidateMbid(null)).toBeNull()
  })

  it('returns null when candidates is null', () => {
    expect(firstCandidateMbid(artistWithCandidates(null))).toBeNull()
  })

  it('returns null when candidates is empty', () => {
    expect(firstCandidateMbid(artistWithCandidates([]))).toBeNull()
  })

  it('returns null when first candidate is null', () => {
    expect(firstCandidateMbid(artistWithCandidates([null]))).toBeNull()
  })

  it('returns null when mbid is missing', () => {
    expect(
      firstCandidateMbid(artistWithCandidates([{ name: 'Prince' }])),
    ).toBeNull()
  })

  it('returns null when mbid is not a string (defensive)', () => {
    expect(
      firstCandidateMbid(artistWithCandidates([{ mbid: 42 }])),
    ).toBeNull()
  })

  it('returns the mbid string when present on the first candidate', () => {
    expect(
      firstCandidateMbid(
        artistWithCandidates([
          { mbid: 'mbid-prince-xxx', name: 'Prince' },
          { mbid: 'mbid-other', name: 'Prince (Other)' },
        ]),
      ),
    ).toBe('mbid-prince-xxx')
  })
})
