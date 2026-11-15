"""Listening in voice chat: notice when someone starts talking, collect what they say,
and turn it into text with faster-whisper (runs locally, nothing leaves the server).

The bot side (main.py) gives a ListenSink two callbacks:
    on_speech_start(member)       someone just started talking -> stop the bot's own speech
    on_utterance(member, pcm)     they finished a sentence -> transcribe and answer it

Both are called on the bot's event loop. py-cord feeds the sink from its own receive
thread, so everything here that runs in write() must stay cheap.

Audio from py-cord arrives as 20ms frames of 48kHz 16-bit stereo PCM.
"""
import os
import threading
import time

import numpy as np

from discord.sinks import Sink

FRAME_MS = 20
SAMPLE_RATE = 48000
CHANNELS = 2
# voice activity detection, by loudness. tuned for discord's normalised mic audio
SPEECH_RMS = float(os.getenv("VOICE_SPEECH_RMS") or 500)   # a frame this loud counts as speech
START_FRAMES = 3            # this many loud frames in a row (60ms) starts an utterance
BARGE_IN_MS = 300           # loud speech needed before the bot gets cut off, so a cough doesn't
END_SILENCE_MS = 900        # this much quiet ends it
MIN_UTTERANCE_MS = 400      # shorter blips (coughs, clicks) are thrown away
MAX_UTTERANCE_MS = 20000    # and a monologue is cut into pieces this long
PRE_ROLL_FRAMES = 10        # keep 200ms from before the start, so the first syllable survives

WHISPER_MODEL = os.getenv("WHISPER_MODEL") or "base"     # tiny, base, small, medium, large-v3...
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE") or "auto"   # auto picks cuda when there's a gpu
# whisper makes these up from silence or noise, so on their own they aren't real speech
HALLUCINATIONS = {"", "you", "thank you", "thank you.", "thanks for watching!", "thanks for watching.",
                  "bye.", "bye", ".", "...", "okay.", "so", "uh", "um", "hmm"}


def frame_rms(pcm):
    if not pcm:
        return 0.0
    samples = np.frombuffer(pcm, dtype=np.int16)
    if not samples.size:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


class _Speaker:
    """Per-person state. Touched only from py-cord's receive thread."""

    def __init__(self):
        self.pre_roll = []
        self.frames = []
        self.loud_run = 0
        self.speaking = False
        self.quiet_ms = 0
        self.speech_ms = 0
        self.loud_ms = 0
        self.barged = False


class ListenSink(Sink):
    """A py-cord sink that turns the incoming audio into utterances."""

    # what py-cord's receive machinery expects a sink to have (the released base class
    # doesn't define them, which is part of why recording crashes out of the box)
    __sink_listeners__ = []

    def __init__(self, vc, loop, on_speech_start, on_utterance):
        super().__init__()
        self.vc = vc
        self.loop = loop
        self.on_speech_start = on_speech_start
        self.on_utterance = on_utterance
        self.speakers = {}
        self.lock = threading.Lock()
        self.ignored = set()   # user ids never to listen to (the bot itself, other bots)

    # ---- the interface py-cord calls ----

    @property
    def client(self):
        return self.vc

    def is_opus(self):
        return False   # we want decoded pcm, not opus packets

    def walk_children(self):
        return []

    def cleanup(self):
        self.speakers.clear()

    def write(self, data, user):
        member = data.source if data is not None else None
        uid = getattr(member, "id", None)
        if uid is None or uid in self.ignored or getattr(member, "bot", False):
            return
        pcm = data.pcm
        if not pcm:
            return
        with self.lock:
            self._feed(uid, member, pcm)

    # ---- voice activity detection ----

    def _feed(self, uid, member, pcm):
        s = self.speakers.get(uid)
        if s is None:
            s = self.speakers[uid] = _Speaker()

        loud = frame_rms(pcm) >= SPEECH_RMS
        if not s.speaking:
            s.pre_roll.append(pcm)
            if len(s.pre_roll) > PRE_ROLL_FRAMES:
                s.pre_roll.pop(0)
            s.loud_run = s.loud_run + 1 if loud else 0
            if s.loud_run >= START_FRAMES:
                s.speaking = True
                s.frames = list(s.pre_roll)
                s.pre_roll = []
                s.quiet_ms = 0
                s.speech_ms = len(s.frames) * FRAME_MS
                s.loud_ms = START_FRAMES * FRAME_MS
                s.barged = False
            return

        s.frames.append(pcm)
        s.speech_ms += FRAME_MS
        s.quiet_ms = 0 if loud else s.quiet_ms + FRAME_MS
        if loud:
            s.loud_ms += FRAME_MS
        if not s.barged and s.loud_ms >= BARGE_IN_MS:
            s.barged = True   # they're really talking, not coughing: cut the bot off
            self.loop.call_soon_threadsafe(self.on_speech_start, member)

        if s.quiet_ms >= END_SILENCE_MS or s.speech_ms >= MAX_UTTERANCE_MS:
            audio = b"".join(s.frames)
            spoken = s.speech_ms - s.quiet_ms
            s.speaking = False
            s.frames = []
            s.loud_run = 0
            s.speech_ms = 0
            if spoken >= MIN_UTTERANCE_MS:
                self.loop.call_soon_threadsafe(self.on_utterance, member, audio)


# ---- speech to text ----

_model = None
_model_lock = threading.Lock()


def _get_model():
    """Load whisper once, on first use (the model downloads the first time, ~150MB for base)."""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            device = WHISPER_DEVICE
            started = time.time()
            try:
                _model = WhisperModel(WHISPER_MODEL, device=device,
                                      compute_type="int8_float16" if device == "cuda" else "int8")
            except Exception as e:
                if device == "cpu":
                    raise
                # cuda present but its libraries aren't (common on windows): fall back to cpu
                print(f"[voice] whisper on {device} failed ({e}), using cpu")
                _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            print(f"[voice] whisper '{WHISPER_MODEL}' loaded in {time.time() - started:.1f}s")
        return _model


def pcm_to_whisper(pcm):
    """48kHz 16-bit stereo -> 16kHz float32 mono, which is what whisper wants."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if CHANNELS == 2:
        samples = samples[: len(samples) // 2 * 2].reshape(-1, 2).mean(axis=1)
    # 48k -> 16k: average each group of 3 samples (a crude low-pass, plenty for speech)
    samples = samples[: len(samples) // 3 * 3].reshape(-1, 3).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32)


def _run(model, audio):
    segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True, condition_on_previous_text=False)
    return list(segments)   # it's lazy: the real work (and any cuda error) happens here


def transcribe(pcm):
    """Blocking. Returns the recognised text ("" when it was noise). Run it in a thread."""
    global _model
    audio = pcm_to_whisper(pcm)
    try:
        segments = _run(_get_model(), audio)
    except RuntimeError as e:
        # the gpu was found but its cuda/cublas/cudnn libraries weren't: only shows up on
        # the first real transcription, so swap to cpu here and try once more
        if not any(k in str(e).lower() for k in ("cuda", "cublas", "cudnn")):
            raise
        print(f"[voice] whisper gpu failed ({e}), switching to cpu")
        from faster_whisper import WhisperModel
        with _model_lock:
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments = _run(_model, audio)
    parts = []
    for seg in segments:
        # segments whisper itself thinks are probably not speech are where it hallucinates
        if seg.no_speech_prob > 0.6 or seg.avg_logprob < -1.2:
            continue
        parts.append(seg.text.strip())
    text = " ".join(p for p in parts if p).strip()
    return "" if text.lower() in HALLUCINATIONS else text


def preload():
    """Load the model in the background so the first sentence isn't slow."""
    threading.Thread(target=_get_model, daemon=True, name="whisper-preload").start()
