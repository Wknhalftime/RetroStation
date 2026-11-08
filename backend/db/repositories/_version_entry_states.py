"""The two ways a copied ``version_entry_states`` row meets one already there (phase 2b, D6).

Shared by every adapter that copies states, so the rule is written once.
"""

# Copies of one song (a works merge, the fold): dismissed if either was, seen at the later
# time. ``state`` is 'dismissed' or NULL (0050's CHECK), so COALESCE keeps a dismissal.
ON_CONFLICT_COMBINE = """ON CONFLICT (work_id, version_text) DO UPDATE SET
    state = COALESCE(version_entry_states.state, EXCLUDED.state),
    seen_at = GREATEST(version_entry_states.seen_at, EXCLUDED.seen_at)"""

# Lines carried to another song (split, reassign, move_file_to_work): the copy arrives seen
# only, so the target keeps its own state, dismissed or not, seen at the later time.
ON_CONFLICT_KEEP_STATE = """ON CONFLICT (work_id, version_text) DO UPDATE SET
    seen_at = GREATEST(version_entry_states.seen_at, EXCLUDED.seen_at)"""
