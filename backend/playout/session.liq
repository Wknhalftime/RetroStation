# RetroStation tune-in session: one Liquidsoap 2.4.5 process per listener (spec D6, D7).
# Three decks (A/B/C) are mixed with add(normalize=false). When the playing deck reaches
# its start-next point (source.on_position callback) the next prefetched item goes onto
# the next deck. Never poll from thread.run: that froze the stream in the spike.
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

def note(event, detail) =
  print("RS #{event} session=#{session_id} #{detail} t=#{time()}")
end

def post(route) =
  try
    ignore(http.post(timeout=5., headers=auth, data="", "#{base}#{route}"))
  catch err do
    note("POST_FAILED", "route=#{route} err=#{err.kind}")
  end
end

# ---- decks (defined first: fetch checks each file against a deck's content type) ----
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

# ---- items: GET /items/<seq>; the same seq always returns the same item ----
next_seq = ref(0)
ended = ref(false)

def rec fetch() =
  seq = next_seq()
  try
    resp = http.get(timeout=10., headers=auth, "#{base}/items/#{seq}")
    if
      resp.status_code == 410
    then
      ended := true
      note("END_OF_SCHEDULE", "seq=#{seq}")
      null
    elsif
      resp.status_code != 200
    then
      note("ITEM_UNAVAILABLE", "seq=#{seq} code=#{resp.status_code}")
      null
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
      next_seq := seq + 1
      # content_type: a plain resolve accepts a corrupt file, which then fails at playback.
      if
        request.resolve(content_type=qa, r)
      then
        note("FETCHED", "seq=#{seq}")
        r
      else
        note("RESOLVE_FAILED", "seq=#{seq}")
        post("/items/#{seq}/failed")
        fetch()
      end
    end
  catch err do
    note("FETCH_ERROR", "seq=#{seq} err=#{err.kind}")
    null
  end
end

# ---- prefetch: keep up to 2 resolved requests ready so a start-next push is instant ----
# top_up runs from several async callbacks; the fetch_lock guard keeps two of them from
# fetching at once (mutexify does not exist in 2.4.5).
pending = ref([])
fetch_lock = ref(false)

def top_up() =
  if
    not fetch_lock()
  then
    fetch_lock := true
    def rec loop() =
      if
        list.length(pending()) < 2 and not ended()
      then
        r = fetch()
        if
          null.defined(r)
        then
          pending := [...pending(), null.get(r)]
          loop()
        end
      end
    end
    loop()
    fetch_lock := false
  end
end

next_deck = ref(0)
starved = ref(false)

def start_next(why) =
  p = pending()
  if
    p == []
  then
    starved := true
    note("STARVED", "why=#{why}")
  else
    pending := list.tl(p)
    i = next_deck()
    next_deck := (i + 1) mod 3
    q = list.nth(queues, i)
    q.push(list.hd(p))
    starved := false
    note("PUSH", "deck=#{list.nth(names, i)} why=#{why}")
  end
end

def wire(q, name) =
  sn_rem = ref(0.)
  fired = ref(false)
  q.on_track(
    synchronous=true,
    fun (m) ->
      begin
        sn_rem := float_of_string(default=0., m["sn_rem"])
        fired := false
        note("STARTED", "deck=#{name} seq=#{m['item_seq']}")
      end
  )
  q.on_track(
    synchronous=false,
    fun (m) ->
      begin
        post("/items/#{m['item_seq']}/started")
        top_up()
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
        start_next("startnext_#{name}")
      end
  )
end
wire(qa, "A")
wire(qb, "B")
wire(qc, "C")
music = add(id="decks", normalize=false, [sa, sb, sc])

# ---- intro layer: one SFX file; the first song starts INTRO_FADE_AT s into it ----
sfx = request.queue(id="sfx")
sfx_out = fade.out(id="fo_sfx", duration=0., track_sensitive=true, sfx)
sfx.on_position(
  synchronous=true,
  allow_partial=true,
  position={intro_fade_at},
  fun (_, _) -> start_next("intro")
)
layers = add(id="layers", normalize=false, [music, sfx_out])

# ---- starvation and end of schedule: the looping filler's track boundaries drive retries ----
filler = single(id="filler", filler_file)
filler.on_track(
  synchronous=false,
  fun (_) ->
    if
      ended() and pending() == []
    then
      note("SCHEDULE_DONE", "")
      shutdown()
    elsif
      starved()
    then
      top_up()
      start_next("filler_retry")
    end
)
main = fallback(id="main_or_filler", track_sensitive=false, [layers, filler])
s = compress(threshold=-18., ratio=3., attack=20., release=300., main)
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
def start_intro_or_first_song() =
  if
    intro_sfx == ""
  then
    start_next("boot")
  else
    intro = request.create("annotate:liq_fade_out=\"1.5\":#{intro_sfx}")
    if
      request.resolve(content_type=sfx, intro)
    then
      sfx.push(intro)
      note("INTRO", "file=#{intro_sfx}")
    else
      note("INTRO_UNAVAILABLE", "file=#{intro_sfx}")
      start_next("boot")
    end
  end
end

o.on_start(
  synchronous=false,
  fun () ->
    begin
      top_up()
      start_intro_or_first_song()
    end
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
