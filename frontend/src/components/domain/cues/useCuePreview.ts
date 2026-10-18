import { useCallback, useEffect, useRef, useState } from "react";
import { gainAt, type Markers, type View } from "./cueMath";

// Plays a stretch of decoded audio with the radio's fade (C1): full gain until Fade-out, then a
// straight line down to silence exactly at Stop. Prop-driven (the caller fetches the bytes), so
// station liners can reuse it with another source.

export interface CuePreview {
  /** True once the audio has decoded and the browser can play it. */
  ready: boolean;
  /** Why the preview can never become ready (no Web Audio, or audio it could not decode). */
  error: string | null;
  /** Where the preview is in the song while it plays, in ms; null when stopped. */
  playheadMs: number | null;
  play(range: View, markers: Markers): void;
  stop(): void;
}

export const NO_WEB_AUDIO = "This browser can't play the preview.";
export const UNDECODABLE = "The browser couldn't decode this audio, so it can't be previewed.";
export const NOT_RESUMED = "The browser wouldn't start the audio.";

interface Decoded {
  ctx: AudioContext;
  buffer: AudioBuffer;
}

interface Playing {
  source: AudioBufferSourceNode;
  gain: GainNode;
  frame: number | null;
}

/** Read at use time, not module load, so a page or test can provide it late. */
function audioContextClass(): typeof AudioContext | undefined {
  return (window as { AudioContext?: typeof AudioContext }).AudioContext;
}

/**
 * The fade on the timeline of a playback that began at `t0` (context seconds) from `fromMs`:
 * hold the gain the radio has at Fade-out's moment, then ramp to 0 exactly at Stop. A preview
 * that starts inside the fade starts at that point of the ramp.
 */
function scheduleFade(param: AudioParam, t0: number, fromMs: number, m: Markers): void {
  const level = fromMs < m.fadeAt ? 1 : gainAt(fromMs, m);
  param.setValueAtTime(level, t0 + Math.max(0, m.fadeAt - fromMs) / 1000);
  param.linearRampToValueAtTime(0, t0 + (m.stop - fromMs) / 1000);
}

function halt(playing: Playing): void {
  if (playing.frame !== null) cancelAnimationFrame(playing.frame);
  playing.source.onended = null;
  playing.source.stop();
  playing.source.disconnect();
  playing.gain.disconnect();
}

export function useCuePreview(audio: ArrayBuffer | undefined): CuePreview {
  const [decoded, setDecoded] = useState<Decoded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playheadMs, setPlayheadMs] = useState<number | null>(null);
  const playing = useRef<Playing | null>(null);

  const stopCurrent = useCallback(() => {
    if (playing.current) halt(playing.current);
    playing.current = null;
  }, []);

  useEffect(() => {
    if (!audio) return;
    const Ctor = audioContextClass();
    if (!Ctor) {
      setError(NO_WEB_AUDIO);
      return;
    }
    const ctx = new Ctor();
    let live = true;
    // decodeAudioData detaches the bytes it is given; decode a copy so the caller's stay usable.
    ctx.decodeAudioData(audio.slice(0)).then(
      (buffer) => {
        if (live) setDecoded({ ctx, buffer });
      },
      () => {
        if (live) setError(UNDECODABLE);
      }
    );
    return () => {
      live = false;
      stopCurrent();
      setDecoded(null);
      setPlayheadMs(null);
      void ctx.close();
    };
  }, [audio, stopCurrent]);

  const stop = useCallback(() => {
    stopCurrent();
    setPlayheadMs(null);
  }, [stopCurrent]);

  const play = useCallback(
    (range: View, markers: Markers) => {
      // Nothing to play (e.g. the End view lies at or after Stop): never start a source.
      if (!decoded || !(range.to > range.from)) return;
      stopCurrent();
      const { ctx, buffer } = decoded;
      // A context made before any click starts suspended; its clock stands still until resumed,
      // so a schedule made now on currentTime still holds.
      ctx.resume().catch(() => setError(NOT_RESUMED));
      const t0 = ctx.currentTime;
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      const gain = ctx.createGain();
      source.connect(gain);
      gain.connect(ctx.destination);
      scheduleFade(gain.gain, t0, range.from, markers);
      source.start(0, range.from / 1000, (range.to - range.from) / 1000);

      const current: Playing = { source, gain, frame: null };
      playing.current = current;
      const finish = () => {
        if (playing.current !== current) return;
        stopCurrent();
        setPlayheadMs(null);
      };
      source.onended = finish;
      setPlayheadMs(range.from);
      if (typeof requestAnimationFrame !== "function") return;
      const tick = () => {
        const ms = range.from + (ctx.currentTime - t0) * 1000;
        if (ms >= range.to) {
          finish();
          return;
        }
        setPlayheadMs(ms);
        current.frame = requestAnimationFrame(tick);
      };
      current.frame = requestAnimationFrame(tick);
    },
    [decoded, stopCurrent]
  );

  return { ready: decoded !== null, error, playheadMs, play, stop };
}
