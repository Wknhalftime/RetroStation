# RetroStation tune-in session: one Liquidsoap 2.4.5 process per listener (spec D6, D7).
# Three decks (A/B/C) are mixed with add(normalize=false). When the playing deck reaches
# its start-next point (source.on_position callback) the next prefetched item goes onto
# the next deck.
#
# Threads. The streaming thread owns every playback ref: the prefetch buffer, the seq
# counter, the decks, and the booted/starved/awaiting state. Only synchronous source
# callbacks read or write them, so pops and pushes never race (mutexify does not exist in
# 2.4.5). Worker threads (thread.run) do the blocking work, HTTP and request resolution,
# never on the streaming thread. A worker hands its result back by writing a ref that
# only it writes, then ringing the bell: a silent source whose on_metadata callback runs
# on the streaming thread. Rings carry no data and may merge, so the bell handler
# re-reads all state. No polling: a periodic thread.run froze the stream in the spike.
# One-shot timers only ring the bell.
#
# Env: SESSION_ID, HARBOR_PORT, BACKEND_URL (.../internal/stream/sessions/<id>),
#      SESSION_TOKEN, FILLER, INTRO_SFX (may be empty), INTRO_FADE_AT, NO_CLIENT_EXIT_S.
settings.log.stdout := true
settings.log.file := false
settings.log.level := 3
settings.harbor.bind_addrs := ["127.0.0.1"]

session_id = environment.get("SESSION_ID")
port = int_of_string(environment.get("HARBOR_PORT"))
base = environment.get("BACKEND_URL")
auth = [("X-Session-Token", environment.get("SESSION_TOKEN"))]
filler_file = environment.get("FILLER")
intro_sfx = environment.get(default="", "INTRO_SFX")
intro_fade_at =
  float_of_string(default=1., environment.get(default="1.0", "INTRO_FADE_AT"))
no_client_exit =
  float_of_string(default=15., environment.get(default="15.0", "NO_CLIENT_EXIT_S"))
# A push starts within 0.12 s (spike); one that has not started after this has failed.
play_check_s = 1.

def note(event, detail) =
  print("RS #{event} session=#{session_id} #{detail} t=#{time()}")
end

# ---- worker side: backend calls block, so they never run on the streaming thread ----
# http.post reports refused or failed calls as status codes (5xx), not as errors.
def post(route) =
  try
    resp = http.post(timeout=5., headers=auth, data="", "#{base}#{route}")
    if
      resp.status_code < 200 or resp.status_code >= 300
    then
      note("POST_FAILED", "route=#{route} code=#{resp.status_code}")
    end
  catch err do
    note("POST_FAILED", "route=#{route} err=#{err.kind} msg=#{string.quote(err.message)}")
  end
end

def post_in_background(route) =
  thread.run(fast=false, fun () -> post(route))
end

# ---- the bell: wakes the streaming thread from a worker or a one-shot timer ----
bell = blank(id="bell")
def ring() =
  bell.insert_metadata([("rs_bell", "1")])
end

# ---- decks and the intro queue (defined first: resolves check a file's content type) ----
def mkdeck(name) =
  q = request.queue(id="deck_#{name}")
  s = amplify(1., override="liq_amplify", q)
  s = fade.in(id="fi_#{name}", duration=0., track_sensitive=true, s)
  s = fade.out(id="fo_#{name}", duration=0., track_sensitive=true, s)
  (q, s)
end
let (qa, sa) = mkdeck("A")
let (qb, sb) = mkdeck("B")
let (qc, sc) = mkdeck("C")
queues = [qa, qb, qc]
names = ["A", "B", "C"]
sfx = request.queue(id="sfx")

# ---- fetch worker: GET /items/<seq>; the same seq always returns the same item ----
# Outcomes: "item" (resolved), "skip" (unplayable, reported failed), "end" (410),
# "retry" (any other answer, or no answer).
def fetch_item(seq) =
  try
    resp = http.get(timeout=10., headers=auth, "#{base}/items/#{seq}")
    if
      resp.status_code == 410
    then
      note("END_OF_SCHEDULE", "seq=#{seq}")
      ("end", null)
    elsif
      resp.status_code != 200
    then
      note("ITEM_UNAVAILABLE", "seq=#{seq} code=#{resp.status_code}")
      ("retry", null)
    else
      let json.parse (item :
        {path: string, annotations: [(string * string)] as json.object}
      ) = resp
      ann =
        string.concat(
          separator=",",
          list.map(fun (kv) -> "#{fst(kv)}=#{string.quote(snd(kv))}", item.annotations)
        )
      r = request.create("annotate:#{ann}:#{item.path}")
      # content_type: a plain resolve accepts a corrupt file, which then fails at playback.
      if
        request.resolve(content_type=qa, r)
      then
        note("FETCHED", "seq=#{seq}")
        ("item", r)
      else
        note("RESOLVE_FAILED", "seq=#{seq}")
        post_in_background("/items/#{seq}/failed")
        ("skip", null)
      end
    end
  catch err do
    note("FETCH_ERROR", "seq=#{seq} err=#{err.kind} msg=#{string.quote(err.message)}")
    ("retry", null)
  end
end

# Written only by the one fetch in flight: (ticket, seq, outcome, request).
fetch_result = ref((0, 0, "none", null))

def fetch_in_background(ticket, seq) =
  thread.run(
    fast=false,
    fun () ->
      begin
        let (outcome, r) = fetch_item(seq)
        fetch_result := (ticket, seq, outcome, r)
        ring()
      end
  )
end

# ---- intro worker: resolving reads the file, so it runs off the streaming thread ----
# Written only by the intro worker.
intro_pushed_at = ref(null)
intro_unavailable = ref(false)

def start_intro() =
  intro = request.create("annotate:liq_fade_out=\"1.5\":#{intro_sfx}")
  if
    request.resolve(content_type=sfx, intro)
  then
    sfx.push(intro)
    intro_pushed_at := time()
    note("INTRO", "file=#{intro_sfx}")
    # One-shot: if the intro has not handed over by then, it failed at play time.
    thread.run(delay=intro_fade_at + play_check_s, ring)
  else
    note("INTRO_UNAVAILABLE", "file=#{intro_sfx}")
    intro_unavailable := true
    ring()
  end
end

# ---- streaming-thread state: only synchronous callbacks below touch these ----
prefetched = ref([])  # up to 2 resolved (seq, request) pairs, oldest first
next_seq = ref(0)
ended = ref(false)
fetch_ticket = ref(0)  # fetches started; at most one is in flight
absorbed_ticket = ref(0)  # fetch results taken in
fetch_held = ref(false)  # the backend said "later": wait for a deck start or filler boundary
next_deck = ref(0)
booted = ref(false)
starved = ref(false)
awaiting = ref(null)  # (seq, pushed at, deck index, request) of a push not yet started

def push_next(why) =
  b = prefetched()
  if
    list.is_empty(b)
  then
    starved := true
    note("STARVED", "why=#{why}")
  else
    let (seq, r) = list.hd(b)
    prefetched := list.tl(b)
    i = next_deck()
    next_deck := (i + 1) mod 3
    list.nth(queues, i).push(r)
    starved := false
    awaiting := (seq, time(), i, r)
    note("PUSH", "deck=#{list.nth(names, i)} seq=#{seq} why=#{why}")
    # One-shot: if the item has not started by then, the bell handler fails it.
    thread.run(delay=play_check_s, ring)
  end
end

def boot(why) =
  if
    not booted()
  then
    booted := true
    push_next(why)
  end
end

def absorb_fetch_result() =
  let (ticket, seq, outcome, r) = fetch_result()
  if
    ticket > absorbed_ticket()
  then
    absorbed_ticket := ticket
    if
      outcome == "item"
    then
      prefetched := [...prefetched(), (seq, null.get(r))]
      next_seq := seq + 1
    elsif
      outcome == "skip"
    then
      next_seq := seq + 1
    elsif
      outcome == "end"
    then
      ended := true
    else
      fetch_held := true
    end
  end
end

def fetch_more() =
  if
    fetch_ticket() == absorbed_ticket()
  and
    not fetch_held()
  and
    not ended()
  and
    list.length(prefetched()) < 2
  then
    fetch_ticket := fetch_ticket() + 1
    fetch_in_background(fetch_ticket(), next_seq())
  end
end

# The first song starts INTRO_FADE_AT s into the intro (sfx on_position); without a
# playable intro it starts at once.
def boot_unless_intro_plays() =
  if
    not booted()
  then
    pushed_at = intro_pushed_at()
    if
      intro_sfx == ""
    then
      boot("boot")
    elsif
      intro_unavailable()
    then
      boot("intro_unavailable")
    elsif
      null.defined(pushed_at)
    and
      time() >= null.get(pushed_at) + intro_fade_at + play_check_s
    then
      note("INTRO_FAILED", "file=#{intro_sfx}")
      boot("intro_failed")
    end
  end
end

# A file that resolved at prefetch can still fail when its deck opens it (changed on
# disk): report it and play the next item instead.
def fail_push_that_never_started() =
  pushed = awaiting()
  if
    null.defined(pushed)
  then
    let (seq, pushed_at, i, r) = null.get(pushed)
    if
      time() >= pushed_at + play_check_s
    then
      awaiting := null
      list.nth(queues, i).remove(r)
      note("PLAY_FAILED", "seq=#{seq} deck=#{list.nth(names, i)}")
      post_in_background("/items/#{seq}/failed")
      push_next("play_failed")
    end
  end
end

# The streaming thread's one decision point, run from every synchronous callback that
# may change what should happen next.
def settle() =
  absorb_fetch_result()
  boot_unless_intro_plays()
  fail_push_that_never_started()
  if
    booted() and starved() and not list.is_empty(prefetched())
  then
    push_next("resume")
  end
  fetch_more()
end

# A deck start or a filler boundary lets a held fetch try the backend again.
def retry_fetch() =
  fetch_held := false
  settle()
end

bell.on_metadata(synchronous=true, fun (_) -> settle())

def wire(q, name) =
  sn_rem = ref(0.)
  fired = ref(false)
  q.on_track(
    synchronous=true,
    fun (m) ->
      begin
        seq = m["item_seq"]
        if
          m["sn_rem"] == ""
        then
          note("METADATA_MISSING", "deck=#{name} seq=#{seq} key=sn_rem")
        end
        sn_rem := float_of_string(default=0., m["sn_rem"])
        fired := false
        pushed = awaiting()
        if
          null.defined(pushed)
        then
          let (awaited, _, _, _) = null.get(pushed)
          if seq == "" or seq == "#{awaited}" then awaiting := null end
        end
        note("STARTED", "deck=#{name} seq=#{seq}")
        if
          seq == ""
        then
          note("METADATA_MISSING", "deck=#{name} key=item_seq")
        else
          post_in_background("/items/#{seq}/started")
        end
        retry_fetch()
      end
  )
  q.on_position(
    synchronous=true,
    remaining=true,
    allow_partial=true,
    position={sn_rem()},
    fun (_, _) ->
      if
        not fired()
      then
        fired := true
        push_next("startnext_#{name}")
      end
  )
end
wire(qa, "A")
wire(qb, "B")
wire(qc, "C")
music = add(id="decks", normalize=false, [sa, sb, sc])

# ---- intro layer: one SFX file; the first song starts INTRO_FADE_AT s into it ----
sfx_out = fade.out(id="fo_sfx", duration=0., track_sensitive=true, sfx)
sfx.on_position(
  synchronous=true,
  allow_partial=true,
  position={intro_fade_at},
  fun (_, _) -> boot("intro")
)
layers = add(id="layers", normalize=false, [music, sfx_out])

# ---- starvation and end of schedule: the looping filler's track boundaries drive retries ----
filler = single(id="filler", filler_file)
filler.on_track(
  synchronous=true,
  fun (_) ->
    if
      ended() and list.is_empty(prefetched()) and not null.defined(awaiting())
    then
      note("SCHEDULE_DONE", "")
      thread.run(fun () -> shutdown())
    else
      retry_fetch()
    end
)
main = fallback(id="main_or_filler", track_sensitive=false, [layers, filler])
# The bell is silent and its metadata is dropped: it only has to be pulled every frame.
s = add(id="main_and_bell", normalize=false, [main, source.drop.metadata(bell)])
s = compress(threshold=-18., ratio=3., attack=20., release=300., s)
s = limit(threshold=-1., s)

o =
  output.harbor(
    %ffmpeg(format = "mp3", %audio(codec = "libmp3lame", b = "128k", ar = 44100)),
    port=port,
    mount="stream",
    s
  )

# Boot when the output starts: the listener connects within about 1 s of launch, and the
# harbor burst then carries the intro to it. A process nobody connects to is shut down
# after NO_CLIENT_EXIT_S (below).
o.on_start(
  synchronous=false,
  fun () -> if intro_sfx == "" then ring() else start_intro() end
)

clients = ref(0)
o.on_connect(
  synchronous=true,
  fun (_) ->
    begin
      clients := clients() + 1
      note("CLIENT_CONNECT", "n=#{clients()}")
    end
)
o.on_disconnect(
  synchronous=true,
  fun (_) ->
    begin
      clients := clients() - 1
      note("CLIENT_DISCONNECT", "n=#{clients()}")
      if clients() <= 0 then shutdown() end
    end
)

# One-shot (not polling): a process nobody ever connects to must not live forever.
thread.run(
  delay=no_client_exit,
  fun () ->
    if
      clients() == 0
    then
      note("NO_CLIENT_EXIT", "")
      shutdown()
    end
)
