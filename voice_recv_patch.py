"""Runtime fixes for py-cord 2.8.1's voice receive, so the bot can hear people in VC.

Discord made DAVE (end-to-end encryption) mandatory for voice in March 2026, and the
released py-cord's receive path can't cope with it. The real fix lives in the unmerged
py-cord PR #3159; rather than running the whole bot on that branch, this swaps out only the
four broken pieces, at import time, and only on the exact version they were written for:

1. Transport decrypt (aead_xchacha20_poly1305_rtpsize) chopped a hard-coded 8 bytes off
   every packet instead of the real RTP header extension length, so the audio frame was
   misaligned whenever the extension wasn't exactly 8 bytes.
2. decrypt_rtp stripped the extension a SECOND time from the DAVE-decrypted frame, and
   when DAVE wasn't active (or the speaker's id wasn't known yet) it never stored the
   decrypted audio at all, so every packet was dropped as "No decrypted data".
3. PacketDecoder._decode_packet ran DAVE decrypt again on the already decoded PCM, which
   mangles it (this is the "stray decrypt call" PR #3159 removes).
4. UDPKeepAlive slept 5000 seconds between keepalives instead of 5, so Discord stopped
   sending audio after a few minutes (py-cord issue #3388).

Once py-cord ships a release with working receive, upgrading makes apply() a no-op, and
this file can be deleted.
"""
import logging

import collections

_log = logging.getLogger(__name__)
PATCHED_VERSION = "2.8.1"
applied = False

# what happened to incoming audio packets, for the bot's periodic "[voice]" status line.
# counted from py-cord's receive threads; a plain Counter is fine for rough numbers
STATS = collections.Counter()
DAVE_MAGIC = b"\xfa\xfa"   # every DAVE-encrypted frame ends with this marker
SILENT_PCM = bytes(960 * 2 * 2)   # 20ms of 48kHz 16-bit stereo silence


def take_stats():
    """The counts since the last call, then reset."""
    snapshot = dict(STATS)
    STATS.clear()
    return snapshot


def apply():
    """Install the fixes. Returns True if they were applied (or already were)."""
    global applied
    if applied:
        return True

    import discord
    if discord.__version__ != PATCHED_VERSION:
        print(f"[voice] py-cord is {discord.__version__}, not {PATCHED_VERSION}: "
              "not patching voice receive (check if it works natively now)")
        return False

    from discord import opus
    from discord.voice.packets.core import OPUS_SILENCE
    from discord.voice.receive import reader

    try:
        import davey
    except ImportError:
        davey = None

    decryptor = reader.PacketDecryptor

    # ---- 1. transport decryption: strip the real extension length ----
    def _decrypt_rtp_aead_xchacha20_poly1305_rtpsize(self, packet):
        packet.adjust_rtpsize()
        nonce = packet.nonce + b"\x00" * 20
        STATS["packets"] += 1
        try:
            # in rtpsize mode the extension's 4 byte preamble stays in the header (and is
            # authenticated as AAD), while the extension values are encrypted with the payload
            result = self.box.decrypt(bytes(packet.data), bytes(packet.header), nonce)
        except Exception as exc:
            STATS["transport_fail"] += 1
            raise reader.CryptoError(exc)
        if packet.extended:
            offset = packet.update_extended_header(result)
            result = result[offset:]
        return result

    # ---- 2. DAVE decryption on the clean frame, and keep non-DAVE audio ----
    def decrypt_rtp(self, packet):
        state = self.client._connection
        frame = self._decryptor_rtp(packet)   # transport layer off, extension stripped

        dave = state.dave_session
        encrypted = frame.endswith(DAVE_MAGIC)
        if encrypted:
            uid = state.ssrc_user_map.get(packet.ssrc)
            if dave is None or davey is None or not dave.ready:
                # still mid-handshake (right after joining, or someone joined/left): there's
                # no key yet, so this frame is unreadable. never hand it to opus as audio
                STATS["dave_not_ready"] += 1
                frame = OPUS_SILENCE
            elif not uid:
                # can't pick a key without knowing who's talking; their SPEAKING event
                # will map them shortly, until then these frames are unreadable
                STATS["unknown_speaker"] += 1
                frame = OPUS_SILENCE
            else:
                try:
                    frame = dave.decrypt(uid, davey.MediaType.audio, frame)
                    STATS["dave_ok"] += 1
                except Exception as exc:
                    STATS["dave_fail"] += 1
                    _log.debug("DAVE decrypt failed for %s", uid, exc_info=exc)
                    frame = OPUS_SILENCE
        else:
            STATS["plain"] += 1   # no DAVE marker: a non-e2ee call, or passthrough during a transition

        packet.decrypted_data = frame
        return frame

    # ---- 3. opus decode without the stray DAVE decrypt on pcm ----
    def _decode_packet(self, packet):
        # a single undecodable frame must not escape: py-cord's packet router treats any
        # exception as fatal and stops recording for good, so the bot would just go deaf
        try:
            if packet:
                pcm = self._decoder.decode(packet.decrypted_data, fec=False)
            else:
                next_packet = self._buffer.peek_next()
                if next_packet is not None:
                    pcm = self._decoder.decode(next_packet.decrypted_data, fec=True)
                else:
                    pcm = self._decoder.decode(None, fec=False)
            STATS["decoded"] += 1
        except opus.OpusError as exc:
            STATS["opus_error"] += 1
            _log.debug("opus couldn't decode a frame from ssrc %s", self.ssrc, exc_info=exc)
            pcm = SILENT_PCM
        return packet, pcm

    decryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = _decrypt_rtp_aead_xchacha20_poly1305_rtpsize
    decryptor.decrypt_rtp = decrypt_rtp
    opus.PacketDecoder._decode_packet = _decode_packet

    # ---- 4. keepalive every 5 seconds ----
    reader.UDPKeepAlive.delay = 5

    # ---- 5. stopping twice is fine ----
    # when the receive machinery dies, BOTH of its router threads call stop_recording() on
    # the way out, and the second one raises "You are not recording" over the real error
    from discord.voice.client import VoiceClient
    from discord.utils import MISSING

    original_stop = VoiceClient.stop_recording

    def stop_recording(self):
        if self._reader is MISSING:
            return
        original_stop(self)

    VoiceClient.stop_recording = stop_recording
    VoiceClient.stop_listening = stop_recording

    applied = True
    print(f"[voice] patched py-cord {PATCHED_VERSION} voice receive (DAVE decrypt order, keepalive)")
    return True
