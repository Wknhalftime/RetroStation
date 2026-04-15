from enum import StrEnum


class MatchStatus(StrEnum):
    PENDING         = "pending"
    AUTO_MATCHED    = "auto_matched"
    NEEDS_REVIEW    = "needs_review"
    MANUAL_MATCHED  = "manual_matched"
    AUTO_REJECTED   = "auto_rejected"
    MANUAL_REJECTED = "manual_rejected"


class MatchTier(StrEnum):
    MUSICBRAINZ_ID_EXACT = "musicbrainz_id_exact"
    NORMALIZATION        = "normalization"
    VECTOR               = "vector"
    MUSICBRAINZ_API      = "musicbrainz_api"
    MANUAL               = "manual"
    UNCLASSIFIED         = "unclassified"


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
    SCAN        = "scan"
    ENRICHMENT  = "enrichment"
    INGESTION   = "ingestion"
    RULES_APPLY = "rules_apply"
    MATCHING    = "matching"
    M3U_EXPORT  = "m3u_export"


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
