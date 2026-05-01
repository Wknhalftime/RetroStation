from enum import StrEnum


class MatchStatus(StrEnum):
    PENDING         = "pending"
    AUTO_MATCHED    = "auto_matched"
    NEEDS_REVIEW    = "needs_review"
    MANUAL_MATCHED  = "manual_matched"
    AUTO_REJECTED   = "auto_rejected"
    MANUAL_REJECTED = "manual_rejected"


class MatchTier(StrEnum):
    MUSICBRAINZ_ID_EXACT  = "musicbrainz_id_exact"
    MUSICBRAINZ_ID_SEARCH = "musicbrainz_id_search"
    LOCAL_FILE_FUZZY      = "local_file_fuzzy"
    NORMALIZATION         = "normalization"
    VECTOR                = "vector"
    MUSICBRAINZ_API       = "musicbrainz_api"
    MANUAL                = "manual"
    UNCLASSIFIED          = "unclassified"


class TargetType(StrEnum):
    ARTIST       = "artist"
    WORK         = "work"
    RECORDING    = "recording"
    LIBRARY_FILE = "library_file"


class VersionType(StrEnum):
    ORIGINAL     = "original"
    LIVE         = "live"
    REMASTER     = "remaster"
    REMIX        = "remix"
    RADIO_EDIT   = "radio_edit"
    DEMO         = "demo"
    ACOUSTIC     = "acoustic"
    EXTENDED     = "extended"
    INSTRUMENTAL = "instrumental"
    EXPLICIT     = "explicit"
    CLEAN        = "clean"
    COVER        = "cover"
    EDITION      = "edition"
    ALTERNATE    = "alternate"
    A_CAPPELLA   = "a_cappella"
    FORMAT       = "format"
    UNKNOWN      = "unknown"
    OTHER        = "other"


class EnrichmentStatus(StrEnum):
    PENDING     = "pending"
    CATEGORIZED = "categorized"
    ENRICHED    = "enriched"
    FAILED      = "failed"
    SKIPPED     = "skipped"


class FileStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    DELETED = "deleted"


class ReleaseType(StrEnum):
    ALBUM       = "album"
    SINGLE      = "single"
    EP          = "ep"
    COMPILATION = "compilation"
    LIVE        = "live"
    BROADCAST   = "broadcast"
    OTHER       = "other"


class ReleaseStatus(StrEnum):
    OFFICIAL       = "official"
    PROMOTION      = "promotion"
    BOOTLEG        = "bootleg"
    PSEUDO_RELEASE = "pseudo-release"


class SelectionMethod(StrEnum):
    AUTO   = "auto"
    MANUAL = "manual"


class CatalogSource(StrEnum):
    LOCAL        = "local"
    MUSICBRAINZ  = "musicbrainz"


class TaskType(StrEnum):
    SCAN               = "scan"
    LIBRARY_ENRICHMENT = "library_enrichment"
    MB_ENRICHMENT      = "mb_enrichment"
    INGESTION          = "ingestion"
    RULES_APPLY        = "rules_apply"
    MATCHING           = "matching"
    M3U_EXPORT         = "m3u_export"


class TaskStatus(StrEnum):
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    TIMEOUT   = "timeout"


# uppercase intentional: matches Python logging, structlog, and external sink conventions
class LogLevel(StrEnum):
    DEBUG   = "DEBUG"
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"


class LogCategory(StrEnum):
    SCAN        = "scan"
    ENRICHMENT  = "enrichment"
    INGESTION   = "ingestion"
    MATCHING    = "matching"
    RULES_APPLY = "rules_apply"
    M3U_EXPORT  = "m3u_export"
    SYSTEM      = "system"


# uppercase intentional: stable persisted keys for why a match is in NEEDS_REVIEW.
# Values are written to broadcast_artists.reason_code / track_identities.reason_code;
# do not rename — they are queried by telemetry and asserted in characterization tests.
class ReasonCode(StrEnum):
    LOW_CONFIDENCE         = "LOW_CONFIDENCE"
    AMBIGUOUS_GAP          = "AMBIGUOUS_GAP"
    DEFERRED_RETRY         = "DEFERRED_RETRY"
    NO_CANDIDATES          = "NO_CANDIDATES"
    NO_LOCAL_FILES         = "NO_LOCAL_FILES"
    MB_SEARCH_INCONCLUSIVE = "MB_SEARCH_INCONCLUSIVE"
    MISSING_MATCH_RECORD   = "MISSING_MATCH_RECORD"
    ORPHANED_IDENTITY      = "ORPHANED_IDENTITY"
    USER_UNMATCHED         = "USER_UNMATCHED"
