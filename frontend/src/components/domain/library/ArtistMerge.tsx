import { useId, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useArtistPick, useMergeArtist, useUnmergeArtist } from "@/api/artists";
import { useDebounce } from "@/lib/useDebounce";
import type { ArtistDetail, MergedMember } from "@/lib/schemas/artists";

// Merge and Unmerge on the artist page (phase 2a S11, M33). Merging is always the curator's:
// pick another artist by name or MBID, confirm, and the page opens the primary. Unmerge deletes
// one member's edge; the members merged into it leave with it, and the confirm names them.

// Set when the page opens a primary after a merge or a merged artist's redirect: the banner
// names that artist while it is still a member here.
export interface MergedFromState {
  mergedFrom?: { id: string; name: string };
}

export function MergePicker({ artist }: { artist: ArtistDetail }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [reason, setReason] = useState("");
  const settled = useDebounce(query, 300);
  const search = useArtistPick(open ? settled : "");
  const merge = useMergeArtist();
  const navigate = useNavigate();
  const panelId = useId();
  const candidates = (search.data?.items ?? []).filter((candidate) => candidate.id !== artist.id);
  const searched = settled.trim() !== "" && !search.isFetching && !search.isError;

  const pick = (primaryId: string, primaryName: string) => {
    const question =
      `Merge ${artist.name} into ${primaryName}? ${artist.name} leaves the artist list, and its ` +
      `works and files show under ${primaryName}. Unmerge undoes it.`;
    if (!window.confirm(question)) return;
    merge.mutate(
      { artistId: artist.id, primaryId, reason: reason.trim() },
      {
        onSuccess: () =>
          navigate(`/library/artists/${primaryId}`, {
            state: { mergedFrom: { id: artist.id, name: artist.name } } satisfies MergedFromState,
          }),
      }
    );
  };

  return (
    <section className="mb-6">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={panelId}
        className="rounded-md border border-gray-200 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50"
      >
        Merge into another artist…
      </button>
      {open && (
        <div id={panelId} className="mt-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
          <label className="block text-xs font-medium text-gray-500">
            The artist to merge into: a name or an MBID
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-200 px-3 py-1.5 text-sm"
            />
          </label>
          <label className="mt-3 block text-xs font-medium text-gray-500">
            Reason (optional)
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-200 px-3 py-1.5 text-sm"
            />
          </label>
          <div aria-live="polite" className="mt-3 text-sm">
            {search.isError && (
              <p role="alert" className="rounded-md bg-red-50 p-3 text-red-700">
                The search failed: {search.error.message}
              </p>
            )}
            {searched && candidates.length === 0 && (
              <p className="text-gray-400">No other artist found.</p>
            )}
            {merge.isPending && <p className="text-gray-500">Merging…</p>}
          </div>
          <ul className="mt-2 divide-y divide-gray-100">
            {candidates.map((candidate) => (
              <li key={candidate.id}>
                <button
                  type="button"
                  onClick={() => pick(candidate.id, candidate.name)}
                  disabled={merge.isPending}
                  className="w-full px-3 py-2 text-left text-sm hover:bg-gray-50 disabled:cursor-wait disabled:opacity-50"
                >
                  {candidate.name}
                  {candidate.disambiguation ? ` (${candidate.disambiguation})` : ""}
                  <span className="ml-2 text-xs text-gray-400">
                    {candidate.work_count} {candidate.work_count === 1 ? "work" : "works"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {merge.isError && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {merge.error.message}
        </p>
      )}
    </section>
  );
}

// The members whose chain of merges passes through *id*: they leave with it on Unmerge.
function leavingWith(id: string, members: MergedMember[]): MergedMember[] {
  const direct = members.filter((member) => member.primary_id === id);
  return direct.flatMap((member) => [member, ...leavingWith(member.id, members)]);
}

export function MergedMembers({ artist }: { artist: ArtistDetail }) {
  const unmerge = useUnmergeArtist();
  if (artist.members.length === 0) return null;

  const confirmUnmerge = (member: MergedMember) => {
    const others = leavingWith(member.id, artist.members);
    const verb = others.length === 1 ? "leaves" : "leave";
    const names = others.map((other) => other.name).join(", ");
    const also = others.length ? ` ${names} ${verb} with it.` : "";
    if (!window.confirm(`Unmerge ${member.name} from ${artist.name}?${also}`)) return;
    unmerge.mutate({
      artistId: member.id,
      leaving: [member.id, ...others.map((other) => other.id)],
    });
  };

  return (
    <section className="mb-6">
      <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500">
        Merged into this artist
      </h2>
      <ul className="divide-y divide-gray-100 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
        {artist.members.map((member) => (
          <li key={member.id} className="flex items-center justify-between px-4 py-2 text-sm">
            <span className="text-gray-900">{member.name}</span>
            <button
              type="button"
              aria-label={`Unmerge ${member.name}`}
              onClick={() => confirmUnmerge(member)}
              disabled={unmerge.isPending}
              className="text-xs text-indigo-600 hover:underline disabled:cursor-wait disabled:opacity-50"
            >
              Unmerge
            </button>
          </li>
        ))}
      </ul>
      {unmerge.isError && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 p-3 text-sm text-red-700">
          {unmerge.error.message}
        </p>
      )}
    </section>
  );
}
