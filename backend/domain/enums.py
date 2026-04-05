from enum import StrEnum


class MatchStatus(StrEnum):
    PENDING       = "PENDING"
    AUTO_MATCHED  = "AUTO_MATCHED"
    NEEDS_REVIEW  = "NEEDS_REVIEW"
    MAN_MATCHED   = "MAN_MATCHED"
    AUTO_REJECTED = "AUTO_REJECTED"
    MAN_REJECTED  = "MAN_REJECTED"


class MatchTier(StrEnum):
    MBID_EXACT      = "MBID_EXACT"
    NORMALIZATION   = "NORMALIZATION"
    VECTOR          = "VECTOR"
    MUSICBRAINZ_API = "MUSICBRAINZ_API"
    MANUAL          = "MANUAL"
    UNKNOWN         = "UNKNOWN"


class TargetType(StrEnum):
    ARTIST       = "Artist"
    WORK         = "Work"
    RECORDING    = "Recording"
    LIBRARY_FILE = "LibraryFile"


class VersionType(StrEnum):
    ORIGINAL     = "ORIGINAL"
    LIVE         = "LIVE"
    REMASTER     = "REMASTER"
    REMIX        = "REMIX"
    RADIO_EDIT   = "RADIO_EDIT"
    DEMO         = "DEMO"
    ACOUSTIC     = "ACOUSTIC"
    EXTENDED     = "EXTENDED"
    INSTRUMENTAL = "INSTRUMENTAL"
    EXPLICIT     = "EXPLICIT"
    COVER        = "COVER"
    EDITION      = "EDITION"
    ALTERNATE    = "ALTERNATE"
    FORMAT       = "FORMAT"
    UNKNOWN      = "UNKNOWN"
    OTHER        = "OTHER"


class EnrichmentStatus(StrEnum):
    PENDING     = "pending"
    CATEGORIZED = "categorized"
    ENRICHED    = "enriched"
    FAILED      = "failed"
    SKIPPED     = "skipped"


class FileStatus(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    DELETED = "DELETED"


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
