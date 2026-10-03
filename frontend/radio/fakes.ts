// Locked test helper for the radio pages (PR F2). Its behaviour is the "fakes.ts behaviour"
// table of plan-f2.md (I7): each fake behaves as the browser object it stands in for, as far as
// the player can observe it, with one deliberate excess: `FakeEventSource.emit` still dispatches
// after the source is closed (a browser never does), so C3 can prove the player's defensive
// check. Nothing here uses a real timer.
import { createPlayer } from "../../backend/web/radio/player.js";
import type { PlayerView } from "../../backend/web/radio/player.js";

type Callback = () => void;

interface PendingTimer {
  id: number;
  due: number;
  callback: Callback;
}

/** Virtual time: callbacks run only inside `advance`, in due order, ties in id order. */
export class FakeTimers {
  now = 0;
  private nextId = 1;
  private live = new Map<number, PendingTimer>();

  set(callback: Callback, ms: number): number {
    const id = this.nextId++;
    this.live.set(id, { id, due: this.now + ms, callback });
    return id;
  }

  clear(id: number): void {
    this.live.delete(id);
  }

  advance(ms: number): void {
    const end = this.now + ms;
    for (;;) {
      let next: PendingTimer | null = null;
      for (const timer of this.live.values()) {
        if (timer.due > end) continue;
        if (next === null || timer.due < next.due || (timer.due === next.due && timer.id < next.id))
          next = timer;
      }
      if (next === null) break;
      this.live.delete(next.id);
      this.now = next.due;
      next.callback();
    }
    this.now = end;
  }

  pending(): number {
    return this.live.size;
  }
}

type Listener<E> = (event: E) => void;

class Listeners<E> {
  private byType = new Map<string, Listener<E>[]>();

  add(type: string, listener: Listener<E>): void {
    const list = this.byType.get(type) ?? [];
    list.push(listener);
    this.byType.set(type, list);
  }

  dispatch(type: string, event: E): void {
    for (const listener of [...(this.byType.get(type) ?? [])]) listener(event);
  }
}

interface PlaySettlement {
  resolve: () => void;
  reject: (reason: unknown) => void;
  settled: boolean;
}

interface QueuedPause {
  timer: number;
  plays: PlaySettlement[];
}

/** Lets every promise reaction queued so far run (microtasks only; no timer). */
async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function abort(plays: PlaySettlement[]): void {
  for (const play of plays) {
    play.settled = true;
    play.reject(new DOMException("The play() request was interrupted", "AbortError"));
  }
}

/**
 * An `<audio>` element, as the player can observe it, following HTML's media element rules:
 * `play()` clears `paused` at once and its promise stays pending until `playing`; `pause()` on a
 * playing element queues a task that fires `pause` and rejects the pending `play()` promises with
 * `AbortError`; `load()` drops that queued task (rejecting its promises at once), stops a playing
 * element and clears `ended`.
 */
export class FakeAudio {
  paused = true;
  ended = false;
  readonly srcWrites: string[] = [];
  removals = 0;
  loads = 0;
  private current = "";
  private readonly plays: PlaySettlement[] = [];
  private queued: QueuedPause[] = [];
  private readonly listeners = new Listeners<Event>();

  constructor(private readonly timers: FakeTimers) {}

  get src(): string {
    return this.current;
  }

  set src(value: string) {
    this.current = value;
    this.ended = false; // a new resource has not ended
    this.srcWrites.push(value);
  }

  /** How many times `play()` was called. */
  get playCalls(): number {
    return this.plays.length;
  }

  removeAttribute(name: string): void {
    if (name === "src") {
      this.current = "";
      this.removals++;
    }
  }

  load(): void {
    for (const task of this.queued) {
      this.timers.clear(task.timer);
      abort(task.plays);
    }
    this.queued = [];
    if (!this.paused) {
      this.paused = true;
      abort(this.takePending());
    }
    this.ended = false;
    this.loads++;
  }

  play(): Promise<void> {
    this.paused = false;
    return new Promise<void>((resolve, reject) => {
      this.plays.push({ resolve, reject, settled: false });
    });
  }

  /** As a browser does: on a playing element, `paused` at once, then a queued `pause` task. */
  pause(): void {
    if (this.paused) return;
    this.paused = true;
    const plays = this.takePending();
    const task: QueuedPause = { timer: 0, plays };
    task.timer = this.timers.set(() => {
      this.queued = this.queued.filter((queued) => queued !== task);
      this.listeners.dispatch("pause", new Event("pause"));
      abort(plays);
    }, 0);
    this.queued.push(task);
  }

  addEventListener(type: string, listener: Listener<Event>): void {
    this.listeners.add(type, listener);
  }

  async resolvePlay(index: number): Promise<void> {
    this.pendingAt(index).resolve();
    await settle();
  }

  /** A refusal; `NotAllowedError` means the browser never started, so `paused` stays `true`. */
  async rejectPlay(index: number, name: string): Promise<void> {
    const play = this.pendingAt(index);
    if (name === "NotAllowedError") this.paused = true;
    play.reject(new DOMException("play() refused", name));
    await settle();
  }

  /** Playback started: `paused` is `false`, pending `play()` promises resolve, then `playing`. */
  firePlaying(): void {
    this.paused = false;
    for (const play of this.takePending()) play.resolve();
    this.listeners.dispatch("playing", new Event("playing"));
  }

  fireWaiting(): void {
    this.listeners.dispatch("waiting", new Event("waiting"));
  }

  fireError(): void {
    this.listeners.dispatch("error", new Event("error"));
  }

  /** The stream ran out: `ended` and `paused` are set, then `pause`, then `ended`, in one task. */
  fireEnd(): void {
    this.ended = true;
    this.paused = true;
    this.listeners.dispatch("pause", new Event("pause"));
    this.listeners.dispatch("ended", new Event("ended"));
  }

  private takePending(): PlaySettlement[] {
    const pending = this.plays.filter((play) => !play.settled);
    for (const play of pending) play.settled = true;
    return pending;
  }

  private pendingAt(index: number): PlaySettlement {
    const play = this.plays[index];
    if (play === undefined) throw new Error(`play() #${index} was never called`);
    if (play.settled) throw new Error(`play() #${index} was already settled`);
    play.settled = true;
    return play;
  }
}

/** What an `EventSource` listener receives: `data` on a named message, nothing on open/error. */
export interface FakeFrame {
  data?: unknown;
}

/** An `EventSource`, recorded in creation order by the rig. */
export class FakeEventSource {
  /** 0 connecting, 1 open, 2 closed, as `EventSource.readyState`. */
  readyState = 0;
  private readonly listeners = new Listeners<FakeFrame>();

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: Listener<FakeFrame>): void {
    this.listeners.add(type, listener);
  }

  close(): void {
    this.readyState = 2;
  }

  open(): void {
    this.readyState = 1;
    this.listeners.dispatch("open", {});
  }

  /** `closed: false` is a blip the browser retries itself; `true` is a closed stream. */
  fail(closed: boolean): void {
    this.readyState = closed ? 2 : 0;
    this.listeners.dispatch("error", {});
  }

  /**
   * Dispatches a named frame, even when `readyState` is 2. A browser never does (the deliberate
   * excess in the header); it lets C3 prove that the player ignores a source that is no longer
   * current.
   */
  emit(type: string, data: string): void {
    this.listeners.dispatch(type, { data });
  }
}

/** `navigator.mediaSession`, with `makeMetadata` standing in for `new MediaMetadata(init)`. */
export class FakeMediaSession {
  metadata: unknown = null;
  playbackState = "none";
  readonly handlers = new Map<string, () => void>();
  readonly inits: Record<string, unknown>[] = [];

  constructor(private readonly unsupported: readonly string[] = []) {}

  setActionHandler(action: string, handler: (() => void) | null): void {
    if (this.unsupported.includes(action)) {
      throw new TypeError(`The provided value '${action}' is not a valid MediaSessionAction`);
    }
    if (handler === null) this.handlers.delete(action);
    else this.handlers.set(action, handler);
  }

  press(action: string): void {
    const handler = this.handlers.get(action);
    if (handler === undefined) throw new Error(`no handler for the ${action} action`);
    handler();
  }

  makeMetadata(init: { title: string; artist: string }): unknown {
    this.inits.push({ ...init });
    return { ...init };
  }
}

export type StorageMode = "ok" | "failGet" | "failSet";

/** `localStorage`, Map-backed; a mode makes reads or writes throw as a browser does. */
export class FakeStorage {
  readonly values = new Map<string, string>();

  constructor(private readonly mode: StorageMode = "ok") {}

  getItem(name: string): string | null {
    if (this.mode === "failGet") throw new DOMException("storage is blocked", "SecurityError");
    return this.values.get(name) ?? null;
  }

  setItem(name: string, value: string): void {
    if (this.mode === "failSet") throw new DOMException("storage is full", "QuotaExceededError");
    this.values.set(name, value);
  }
}

/** `Uint8Array` `[0, 1, …, n − 1]`: a predictable stand-in for `crypto.getRandomValues`. */
export function bytes(count: number): Uint8Array {
  return Uint8Array.from({ length: count }, (_, i) => i);
}

export interface RigOptions {
  media?: boolean;
  unsupported?: string[];
}

/** A player for KIOA 1995 with the key "k", wired to the fakes above. */
export function playerRig(options: RigOptions = {}) {
  const timers = new FakeTimers();
  const audio = new FakeAudio(timers);
  const sources: FakeEventSource[] = [];
  const views: PlayerView[] = [];
  const media = options.media === false ? null : new FakeMediaSession(options.unsupported);
  const player = createPlayer({
    station: { callLetters: "KIOA", year: 1995 },
    key: "k",
    audio,
    openEvents: (path: string) => {
      const source = new FakeEventSource(path);
      sources.push(source);
      return source;
    },
    timers: {
      set: (callback: () => void, ms: number) => timers.set(callback, ms),
      clear: (handle: number) => timers.clear(handle),
    },
    render: (view: PlayerView) => {
      views.push({ ...view });
    },
    media:
      media === null ? null : { session: media, makeMetadata: (init) => media.makeMetadata(init) },
  });

  function latest(): FakeEventSource {
    const source = sources[sources.length - 1];
    if (source === undefined) throw new Error("no EventSource has been opened");
    return source;
  }

  /** Play, then the events stream opens: the player is tuning in. */
  function connect(): void {
    player.play();
    latest().open();
  }

  /** Connect, then the audio plays: the player is playing. */
  function tuneIn(): void {
    connect();
    audio.firePlaying();
  }

  return { player, audio, timers, media, sources, latest, views, connect, tuneIn };
}

export type PlayerRig = ReturnType<typeof playerRig>;
