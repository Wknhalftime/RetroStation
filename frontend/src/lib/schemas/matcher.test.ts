import { describe, expect, it } from 'vitest'
import {
  MbArtistResultSchema,
  QueueArtistSchema,
  QueueIdentitySchema,
} from './matcher'

describe('QueueIdentitySchema', () => {
  it('accepts new fields', () => {
    const parsed = QueueIdentitySchema.parse({
      id: '00000000-0000-0000-0000-000000000001',
      original_title: 'Purple Rain',
      normalized_title: 'purple rain',
      match_status: 'needs_review',
      match_tier: null,
      confidence_score: 72,
      triage_bucket: 'quick_review',
      reason_code: 'LOW_CONFIDENCE',
      reason_detail: 'Score 72% — below confidence threshold',
    })
    expect(parsed.triage_bucket).toBe('quick_review')
  })

  it('rejects invalid triage_bucket', () => {
    expect(() =>
      QueueIdentitySchema.parse({
        id: '00000000-0000-0000-0000-000000000001',
        original_title: 'x',
        normalized_title: 'x',
        match_status: 'needs_review',
        match_tier: null,
        triage_bucket: 'invalid',
      }),
    ).toThrow()
  })
})

describe('QueueArtistSchema', () => {
  it('accepts new fields including triage_bucket', () => {
    const parsed = QueueArtistSchema.parse({
      id: '00000000-0000-0000-0000-000000000002',
      original_name: 'Prince',
      normalized_name: 'prince',
      match_status: 'needs_review',
      reason_code: 'LOW_CONFIDENCE',
      reason_detail: 'Score 65%',
      triage_bucket: 'quick_review',
      candidates: [],
      identities: [],
    })
    expect(parsed.reason_code).toBe('LOW_CONFIDENCE')
  })
})

describe('MbArtistResultSchema', () => {
  it('accepts the backend shape', () => {
    const parsed = MbArtistResultSchema.parse({
      id: 'mbid-1',
      name: 'Prince',
      score: 100,
      disambiguation: '',
    })
    expect(parsed.id).toBe('mbid-1')
  })

  it('defaults disambiguation when missing', () => {
    const parsed = MbArtistResultSchema.parse({
      id: 'mbid-1',
      name: 'Prince',
      score: 100,
    })
    expect(parsed.disambiguation).toBe('')
  })
})
