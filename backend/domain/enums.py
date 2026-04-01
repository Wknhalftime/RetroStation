from enum import Enum


class MatchStatus(str, Enum):
    PENDING       = "PENDING"
    AUTO_MATCHED  = "AUTO_MATCHED"
    NEEDS_REVIEW  = "NEEDS_REVIEW"
    MAN_MATCHED   = "MAN_MATCHED"
    AUTO_REJECTED = "AUTO_REJECTED"
    MAN_REJECTED  = "MAN_REJECTED"


class MatchTier(str, Enum):
    MBID_EXACT      = "MBID_EXACT"
    NORMALIZATION   = "NORMALIZATION"
    VECTOR          = "VECTOR"
    MUSICBRAINZ_API = "MUSICBRAINZ_API"
    MANUAL          = "MANUAL"
    UNKNOWN         = "UNKNOWN"


class TargetType(str, Enum):
    ARTIST       = "Artist"
    WORK         = "Work"
    RECORDING    = "Recording"
    LIBRARY_FILE = "LibraryFile"


class VersionType(str, Enum):
    ORIGINAL   = "ORIGINAL"
    LIVE       = "LIVE"
    REMASTER   = "REMASTER"
    REMIX      = "REMIX"
    RADIO_EDIT = "RADIO_EDIT"
    DEMO       = "DEMO"
    ACOUSTIC   = "ACOUSTIC"
    OTHER      = "OTHER"


class EnrichmentStatus(str, Enum):
    PENDING     = "pending"
    CATEGORIZED = "categorized"
    ENRICHED    = "enriched"
    FAILED      = "failed"
    SKIPPED     = "skipped"


class ReleaseType(str, Enum):
    ALBUM       = "album"
    SINGLE      = "single"
    EP          = "ep"
    COMPILATION = "compilation"
    LIVE        = "live"
    BROADCAST   = "broadcast"
    OTHER       = "other"


class ReleaseStatus(str, Enum):
    OFFICIAL       = "official"
    PROMOTION      = "promotion"
    BOOTLEG        = "bootleg"
    PSEUDO_RELEASE = "pseudo-release"


class SelectionMethod(str, Enum):
    AUTO   = "auto"
    MANUAL = "manual"


class TaskType(str, Enum):
    SCAN        = "scan"
    ENRICHMENT  = "enrichment"
    INGESTION   = "ingestion"
    RULES_APPLY = "rules_apply"
    MATCHING    = "matching"
    M3U_EXPORT  = "m3u_export"


class TaskStatus(str, Enum):
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    TIMEOUT   = "timeout"
