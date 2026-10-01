"""vulture whitelist: code that is called by a framework, not by our code.

Generated with `vulture backend --make-whitelist`, then limited by hand to
FastAPI route handlers, Huey periodic tasks and Huey startup hooks, each checked
against its decorator and against routers/v1.py mounting its router. Anything else
vulture reports is a finding, not a whitelist entry. Regenerate the same way when routes move.
"""

# FastAPI route handlers (registered by @app/@router decorators)
health  # backend/main.py
ws  # backend/main.py
upload_playlist  # backend/routers/ingestion.py
list_artists  # backend/routers/library/artists.py
get_artist_detail  # backend/routers/library/artists.py
list_files  # backend/routers/library/files.py
scan_library  # backend/routers/library/scan.py
get_library_status  # backend/routers/library/status.py
get_work_detail  # backend/routers/library/works.py
set_work_master  # backend/routers/library/works.py
delete_work_master  # backend/routers/library/works.py
create_format_override  # backend/routers/library/works.py
delete_format_override  # backend/routers/library/works.py
merge_works  # backend/routers/library/works.py
split_work  # backend/routers/library/works.py
reassign_file_work  # backend/routers/library/works.py
get_matching_queue  # backend/routers/matching.py
resolve_artist  # backend/routers/matching.py
resolve_identity  # backend/routers/matching.py
unmatch_artist  # backend/routers/matching.py
unmatch_identity  # backend/routers/matching.py
run_matching  # backend/routers/matching.py
search_mb_artists  # backend/routers/matching.py
list_playlists  # backend/routers/playlists.py
get_playlist  # backend/routers/playlists.py
get_playlist_events  # backend/routers/playlists.py
get_broadcast_days  # backend/routers/playlists.py
export_m3u  # backend/routers/playlists.py
get_all_settings  # backend/routers/settings.py
put_setting  # backend/routers/settings.py
list_stations  # backend/routers/stations.py
create_station  # backend/routers/stations.py
get_station  # backend/routers/stations.py
update_station  # backend/routers/stations.py
delete_station  # backend/routers/stations.py
get_station_broadcast_days  # backend/routers/stations.py
get_station_events_by_date  # backend/routers/stations.py
get_missing_matches_report  # backend/routers/stations.py
export_station_m3u  # backend/routers/stations.py
list_system_logs  # backend/routers/system_logs.py
get_logs_by_trace  # backend/routers/system_logs.py
get_active_tasks  # backend/routers/tasks.py
retry_enrichment  # backend/routers/tasks.py

# Huey periodic tasks (scheduled by the consumer, never called directly)
library_hash_backfill_resume  # backend/tasks/library_hash_backfill_tasks.py
library_watcher_poll  # backend/tasks/library_watcher_tasks.py
stream_cue_analysis_resume  # backend/tasks/stream_cue_tasks.py
stream_cue_prune_task  # backend/tasks/stream_cue_tasks.py

# Huey startup hooks (run by each consumer worker before its first task)
configure_cue_consumer_logging  # backend/tasks/cue_huey_app.py
join_kill_on_close_job  # backend/tasks/cue_huey_app.py
remove_stale_cue_listings  # backend/tasks/stream_cue_tasks.py
