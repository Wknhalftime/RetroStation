"""Huey tasks: an orchestrated command pipeline (AUD-R015), not pub/sub.

Each message is a command to one named consumer carrying only a scope; the
work items live in status columns (see ARCHITECTURE.md). Each consumer runs
exactly one worker (`-w 1`, AUD-R017), and every task-to-task hand-off goes
through `_enqueue_chain.enqueue_or_log` (AUD-R014).
"""
