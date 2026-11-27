"""The linker's evidence sources (artist model phase 4.2 PR 1, SPEC S2, E1; D1; design C2, C8,
C24, C32, C43, C46, C67, M33; 4.1's E5 and R-j; AUD-R095).

Local sources first (C24): the owned-song anchor (C67), exact names through ``artist_names`` and
Merge (C43, M33), and close names, scored word by word (C2) over a shortlist of
``thresholds.shortlist`` (C32), each holding one of the name's played titles in the library.
Only a name the local evidence doesn't link reads MusicBrainz, and only from the cache, by the
key the 4.1 fill writes: its titles most played first, skipping the common ones (C8), until two
titles vote for one credit or one rare title does (D1). A needed entry the cache doesn't hold
makes the name wait (E1). A title-only entry (a name of initials, 4.1's E5) is never a name
signal.

C2 compares words in any order, drops the joining words and "the", "a", "an", never splits a
name into artists, lets a word be the start of a longer one ("O." → Olivia, "BR" → Brothers,
C67), pairs a word with two run together ("BLACKSHEEP", "'N SYNC"), and marks a pair that is
only a near miss. It reports the words a candidate leaves unexplained and the performers of a
credit who explain none (C6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from backend.domain.cache_fill import NamePair
from backend.domain.enums import FillTier, LinkDecision
from backend.domain.linking import Credit, LinkingThresholds, NameEvidence, Performer, decide
from backend.domain.system import MusicBrainzCache
from backend.services.cache_fill_service import _pair_calls
from backend.services.link_evidence import EvidenceSources, fit_name, gather_evidence, log_words
from tests.fakes.link_evidence import FakeLinkEvidenceRepository
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

T = LinkingThresholds(title_lead=1, common_acts=3, shortlist=10, near_miss=0.85, name_max_len=30)


def credit(*performers: Performer) -> Credit:
    return Credit(performers)


A = Performer("mbid:a", "Act A")


# --- C2's words -----------------------------------------------------------------------------------


def test_log_words_drop_joiners_and_articles_and_keep_initials() -> None:
    assert log_words("O. NEWTON-JOHN/J. TRAVOLTA") == ("o", "newton-john", "j", "travolta")
    assert log_words("HALL & OATES") == ("hall", "oates")
    assert log_words("THE EAGLES") == ("eagles",)
    assert log_words("SANTANA FEAT. ROB THOMAS") == ("santana", "rob", "thomas")
    assert log_words("'N SYNC") == ("n", "sync")


def test_an_exact_fit_in_any_order() -> None:
    act = Performer("mbid:st", "Sanford & Townsend")

    for name in ("SANFORD/TOWNSEND", "TOWNSEND SANFORD"):
        fit = fit_name(log_words(name), credit(act), T)
        assert (fit.explained, fit.exact, fit.fuzzy) == (frozenset({0, 1}), True, False), name


def test_articles_and_joiners_in_the_candidate_are_skipped() -> None:
    eagles = fit_name(log_words("EAGLES"), credit(Performer("mbid:e", "The Eagles")), T)
    hall = fit_name(log_words("HALL OATES"), credit(Performer("mbid:h", "Hall & Oates")), T)

    assert (eagles.exact, hall.exact) == (True, True)


def test_initials_and_short_words_start_longer_ones() -> None:
    """C67: "O." → Olivia, "J." → John, "BR" → Brothers; not near misses."""
    duet = credit(
        Performer("mbid:jt", "John Travolta"), Performer("mbid:onj", "Olivia Newton-John")
    )
    brothers = credit(Performer("mbid:cb", "Cornelius Brothers & Sister Rose"))

    for name, target in (
        ("O. NEWTON-JOHN/J. TRAVOLTA", duet),
        ("CORNELIUS BR/SISTER ROSE", brothers),
    ):
        fit = fit_name(log_words(name), target, T)
        words = len(log_words(name))
        assert (fit.explained, fit.fuzzy) == (frozenset(range(words)), False), name


def test_a_word_pairs_with_two_run_together() -> None:
    """BLACKSHEEP is Black Sheep, and 'N SYNC is *NSYNC; neither is a near miss."""
    sheep = fit_name(log_words("BLACKSHEEP"), credit(Performer("mbid:bs", "Black Sheep")), T)
    nsync = fit_name(log_words("'N SYNC"), credit(Performer("mbid:ns", "*NSYNC")), T)

    assert (sheep.explained, sheep.fuzzy) == (frozenset({0}), False)
    assert (nsync.explained, nsync.fuzzy) == (frozenset({0, 1}), False)


def test_a_near_miss_pairs_and_is_marked() -> None:
    fit = fit_name(log_words("LYNYRD SKYNNYRD"), credit(Performer("mbid:ls", "Lynyrd Skynyrd")), T)

    assert (fit.explained, fit.fuzzy) == (frozenset({0, 1}), True)


def test_archies_explains_nothing_of_architects() -> None:
    fit = fit_name(log_words("ARCHIES"), credit(Performer("mbid:ar", "Architects")), T)

    assert fit.explained == frozenset()


def test_leftovers_and_performers_who_explain_nothing_are_reported() -> None:
    """C2 reports leftovers; C6: Rob Thomas explains no word of SANTANA."""
    rob = Performer("mbid:rt", "Rob Thomas")
    fit = fit_name(log_words("SANTANA"), credit(Performer("mbid:s", "Santana"), rob), T)

    assert fit.explained == frozenset({0})
    assert set(fit.leftovers) == {"rob", "thomas"}
    assert fit.bystanders == frozenset({"mbid:rt"})


def test_a_word_left_unexplained_is_reported_not_a_veto() -> None:
    fit = fit_name(log_words("ARCHITECTS UK"), credit(Performer("mbid:ar", "Architects")), T)

    assert (fit.explained, fit.exact) == (frozenset({0}), False)


# --- the local sources --------------------------------------------------------------------------


def world() -> tuple[FakeLinkEvidenceRepository, FakeMusicBrainzCacheRepository, EvidenceSources]:
    evidence, cache = FakeLinkEvidenceRepository(), FakeMusicBrainzCacheRepository()
    return evidence, cache, EvidenceSources(evidence=evidence, cache=cache)


def keys(found: NameEvidence) -> set[tuple[str, ...]]:
    return {tuple(p.key for p in c.credit.performers) for c in found.candidates}


def test_the_anchor_is_a_library_file_of_a_played_title() -> None:
    evidence, cache, sources = world()
    jt = evidence.add_artist("mbid:jt", "John Travolta")
    onj = evidence.add_artist("mbid:onj", "Olivia Newton-John")
    evidence.add_file("you're the one that i want", jt, onj)
    name = uuid4()
    evidence.play(name, "you're the one that i want", 900)

    found = gather_evidence(name, "O. NEWTON-JOHN/J. TRAVOLTA", sources, T)

    [duet] = [c for c in found.candidates if len(c.credit.performers) == 2]
    assert duet.fit.explained == frozenset({0, 1, 2, 3})
    assert [(t.title, t.acts) for t in duet.library_titles] == [("you're the one that i want", 2)]
    assert {(d.kind, d.performer and d.performer.key) for d in decide(found, T)} >= {
        (LinkDecision.LINK, "mbid:jt"),
        (LinkDecision.LINK, "mbid:onj"),
    }


def test_an_exact_name_is_found_through_artist_names() -> None:
    """C43: a MusicBrainz alias or tag spelling ("JOHN COUGAR" → John Mellencamp)."""
    evidence, _, sources = world()
    evidence.add_artist("mbid:jm", "John Mellencamp", also=("John Cougar",))

    found = gather_evidence(uuid4(), "JOHN COUGAR", sources, T)

    [mellencamp] = found.candidates
    assert (keys(found), mellencamp.fit.exact) == ({("mbid:jm",)}, True)


def test_a_merged_artist_is_its_survivor() -> None:
    """M33: a name of the merged artist points at the artist it merged into, once."""
    evidence, _, sources = world()
    evidence.add_artist("mbid:keep", "Prince")
    evidence.add_artist("mbid:gone", "Prince", merged_into="mbid:keep")

    assert keys(gather_evidence(uuid4(), "PRINCE", sources, T)) == {("mbid:keep",)}


def test_a_close_name_needs_a_played_title_in_the_library() -> None:
    evidence, _, sources = world()
    skynyrd = evidence.add_artist("mbid:ls", "Lynyrd Skynyrd")
    evidence.add_artist("mbid:lsb", "Lynyrd Skynyrd Band")
    evidence.add_file("free bird", skynyrd)
    name = uuid4()
    evidence.play(name, "free bird", 50)

    found = gather_evidence(name, "LYNYRD SKYNNYRD", sources, T)

    assert keys(found) == {("mbid:ls",)}
    [close] = found.candidates
    assert close.fit.fuzzy


def test_close_names_come_from_a_shortlist() -> None:
    """C32: only the thresholds' shortlist of the catalog is scored word by word."""
    evidence, _, sources = world()
    name = uuid4()
    for n, spelling in enumerate(("Lynyrd Skynyrd", "Lynyrd Skinyrd", "Lynard Skynard"), start=1):
        act = evidence.add_artist(f"mbid:{n}", spelling)
        evidence.add_file(f"title {n}", act)
        evidence.play(name, f"title {n}", 10)
    two = LinkingThresholds(
        title_lead=1, common_acts=3, shortlist=2, near_miss=0.6, name_max_len=30
    )

    found = gather_evidence(name, "LYNYRD SKYNYRD", sources, two)

    assert keys(found) == {("mbid:1",), ("mbid:2",)}


def test_rarity_is_the_ports_count_of_distinct_acts() -> None:
    """C8: Run-D.M.C. feat. Aerosmith adds nothing to Aerosmith; a log name linked to Aerosmith
    adds nothing either; so "walk this way" is held by 2 acts."""
    evidence, _, sources = world()
    aerosmith = evidence.add_artist("mbid:aero", "Aerosmith")
    rundmc = evidence.add_artist("mbid:run", "Run-D.M.C.")
    evidence.add_file("walk this way", aerosmith)
    evidence.add_file("walk this way", rundmc, aerosmith)
    other = uuid4()
    evidence.play(other, "walk this way", 3)
    evidence.link_active(other, aerosmith)
    name = uuid4()
    evidence.play(name, "walk this way", 40)

    found = gather_evidence(name, "AEROSMITH", sources, T)

    [alone] = [c for c in found.candidates if c.credit == credit(aerosmith)]
    assert [(t.title, t.acts) for t in alone.library_titles] == [("walk this way", 2)]


def test_a_manual_link_is_read_from_the_port() -> None:
    evidence, _, sources = world()
    name = uuid4()
    evidence.link_manual(name)

    assert gather_evidence(name, "ANYONE", sources, T).manual


# --- MusicBrainz, from the cache only ------------------------------------------------------------


def hit(*parts: tuple[str, str]) -> dict[str, Any]:
    """A slim name + title hit (4.1's ``_slim_name_title_hit``): credited (mbid, name) parts."""
    return {
        "id": str(uuid4()),
        "score": 100,
        "title": "t",
        "artist-credit": [
            {"name": name, "joinphrase": "", "artist": {"id": mbid, "name": name}}
            for mbid, name in parts
        ],
    }


def fill_key(original_name: str, title: str, max_len: int = 30) -> str:
    """The key the 4.1 fill writes for this (name, title): its own planner, not a copy."""
    pair = NamePair(
        normalized_name=original_name.lower(),
        original_name=original_name,
        normalized_title=title.lower(),
        title=title,
        plays=1,
        in_library=True,
        common=False,
    )
    [call] = _pair_calls(FillTier.PAYOFF, [pair], max_len)
    return call.key


def cache_hits(
    cache: FakeMusicBrainzCacheRepository,
    key: str,
    hits: list[dict[str, Any]],
    *,
    title_only: bool = False,
) -> None:
    now = datetime.now(tz=UTC)
    cache.set(
        MusicBrainzCache(
            id=uuid4(),
            cache_key=key,
            entity_type="recording-name-title",
            entity_mbid="",
            response_data={"count": len(hits), "title_only": title_only, "recordings": hits},
            cached_at=now,
            expires_at=now + timedelta(days=30),
        )
    )


def played(evidence: FakeLinkEvidenceRepository, *titles: str) -> UUID:
    name = uuid4()
    for n, title in enumerate(titles):
        evidence.play(name, title.lower(), 100 - n, original=title)
    return name


def test_the_linker_reads_the_key_the_fill_writes() -> None:
    """A cut-off name (it fills the 30-character field) and a title with the log's f/ credit:
    both shape the fill's key, so a drifted key would leave every name waiting. A hit covers the
    words the search required, so the cut-off last word ("PRODUCT") needn't be covered."""
    evidence, cache, sources = world()
    original = "CARLOS SANTANA AND THE PRODUCT"
    title = "Maria Maria (f/The Product G&B)"
    name = played(evidence, title)
    santana = ("c1", "Carlos Santana")
    cache_hits(cache, fill_key(original, title), [hit(santana), hit(santana)])

    found = gather_evidence(name, original, sources, T)

    assert not found.waiting
    [votes] = found.musicbrainz
    assert [(c.performers[0].key, n) for c, n in votes.hits] == [("mbid:c1", 2)]


def test_only_hits_covering_every_word_vote() -> None:
    """QUEEN/DAVID BOWIE: Bowie's own live takes cover one word and are dropped."""
    evidence, cache, sources = world()
    name = played(evidence, "Under Pressure")
    duet = hit(("q", "Queen"), ("db", "David Bowie"))
    bowie_live = [hit(("db", "David Bowie"))] * 3
    cache_hits(cache, fill_key("QUEEN/DAVID BOWIE", "Under Pressure"), [*bowie_live, duet])

    found = gather_evidence(name, "QUEEN/DAVID BOWIE", sources, T)

    [votes] = found.musicbrainz
    assert [(tuple(p.key for p in c.performers), n) for c, n in votes.hits] == [
        (("mbid:q", "mbid:db"), 1)
    ]
    assert ("mbid:q", "mbid:db") in keys(found)


def test_reading_stops_at_two_agreeing_titles() -> None:
    evidence, cache, sources = world()
    name = played(evidence, "One", "Two", "Three")
    for title in ("One", "Two"):
        cache_hits(cache, fill_key("ACT A", title), [hit(("a", "Act A"))] * 2)

    found = gather_evidence(name, "ACT A", sources, T)

    assert ([v.title for v in found.musicbrainz], found.waiting) == (["one", "two"], False)


def test_reading_stops_at_one_rare_title() -> None:
    """D1: the title's one local act is Act A, through another log name's active link (so no
    library file anchors it), and its covering hits hold Act A alone."""
    evidence, cache, sources = world()
    act = evidence.add_artist("mbid:a", "Act A")
    other = played(evidence, "Ours Alone")
    evidence.link_active(other, act)
    name = played(evidence, "Ours Alone", "Two")
    cache_hits(cache, fill_key("ACT A", "Ours Alone"), [hit(("a", "Act A"))])

    found = gather_evidence(name, "ACT A", sources, T)

    assert ([v.title for v in found.musicbrainz], found.waiting) == (["ours alone"], False)
    [rare] = found.musicbrainz
    assert (rare.acts, rare.holder) == (1, "mbid:a")


def test_common_titles_are_never_read() -> None:
    """C8: they can't count, so their missing entries make nobody wait."""
    evidence, cache, sources = world()
    for n in range(3):
        evidence.add_file("silent night", Performer(f"mbid:{n}", f"Choir {n}"))
    name = played(evidence, "Silent Night", "One", "Two")
    for title in ("One", "Two"):
        cache_hits(cache, fill_key("ACT A", title), [hit(("a", "Act A"))])

    found = gather_evidence(name, "ACT A", sources, T)

    assert ([v.title for v in found.musicbrainz], found.waiting) == (["one", "two"], False)


def test_a_needed_entry_missing_makes_the_name_wait() -> None:
    """E1: the most-played counting title isn't cached yet."""
    evidence, _, sources = world()
    name = played(evidence, "One", "Two")

    assert gather_evidence(name, "ACT A", sources, T).waiting


def test_a_title_only_entry_is_never_a_name_signal() -> None:
    """4.1's E5: R.E.M. has no search words, so its entry is the title's every recording."""
    evidence, cache, sources = world()
    name = played(evidence, "Losing My Religion")
    cache_hits(
        cache,
        fill_key("R.E.M.", "Losing My Religion"),
        [hit(("rem", "R.E.M."))] * 3,
        title_only=True,
    )

    found = gather_evidence(name, "R.E.M.", sources, T)

    assert all(not v.hits for v in found.musicbrainz)
    assert not found.waiting


def test_musicbrainz_is_not_read_when_the_local_sources_link() -> None:
    """C24: MusicBrainz only for a name still undecided."""
    evidence, cache, sources = world()
    act = evidence.add_artist("mbid:a", "Act A")
    evidence.add_file("one", act)
    name = played(evidence, "One")
    cache_hits(cache, fill_key("ACT A", "One"), [hit(("other", "Act A"))] * 5)

    found = gather_evidence(name, "ACT A", sources, T)

    assert found.musicbrainz == ()
    assert A.key in {d.performer.key for d in decide(found, T) if d.performer is not None}
