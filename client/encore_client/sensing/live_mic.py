"""Qt capture for the audience microphone and timestamped 16 kHz analysis chunks.

With a controlled player, WebRTC processes 48 kHz capture against its playback
probe. The local recording tap receives only unprocessed audience PCM.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

from ..clock import monotonic_ms
from ..session.contract import AUDIO_RATE
from ..session.session import Conn

CHUNK_MS = 500
RESYNC_MS = 250  # timestamps this far from the clock: samples were lost, start a new run
SETTLE_MS = 150  # a device's first samples can be a full-scale pop (measured: ~25 ms on a laptop microphone)
MAX_RECORDING_S = 600


class LiveMicrophone:
    def __init__(self, session, *, chunk_ms: int = CHUNK_MS,
                 clock: Callable[[], int] = monotonic_ms, keep_s: int = MAX_RECORDING_S,
                 player=None, on_pcm=None) -> None:
        if not 0 < chunk_ms <= 1000:
            raise ValueError("chunk_ms must be 1..1000")
        self.session = session
        self.player = player
        self.on_pcm = on_pcm
        self._connected = None
        self.aec_status = "reference_unavailable" if player else "off"
        self._raw_buf = bytearray()
        self._statuses = deque()
        self.chunk_bytes = AUDIO_RATE * chunk_ms // 1000 * 2
        self.sent = 0
        self.recording = bytearray()  # what was captured while the microphone was on, for saving
        self.last_pcm = b""  # the latest device read, for a level meter
        self.zero_ms = 0  # how long the device has delivered exact zeros: a muted input, not a quiet room
        self._keep = keep_s * AUDIO_RATE * 2
        self._clock = clock
        self._buf = bytearray()
        self._run: list[int] | None = None  # [generation, first sample ms, bytes taken from the run]
        self._skip = 0  # bytes still to discard after the device started

    def device_started(self, settle_ms: int = SETTLE_MS) -> None:
        """The device just opened: discard its first ``settle_ms`` of audio."""
        self._skip = AUDIO_RATE * settle_ms // 1000 * 2

    def _reset_run(self):
        self._buf.clear()
        self._raw_buf.clear()
        self._statuses.clear()
        self._run = None

    def local_recording_allowed(self) -> bool:
        """While offline only the explicitly active camera's local audio tap can receive PCM."""
        sensors = self.session.view.sensors
        camera = sensors.get("camera")
        return (self.on_pcm is not None and sensors["microphone"].enabled
                and camera is not None and camera.enabled
                and self.session.state in (Conn.CONNECTING, Conn.DISCONNECTED, Conn.RECONNECTING))

    def connection_changed(self, connected: bool, generation: int) -> bool:
        changed = self._connected is not None and self._connected != connected
        self._connected = connected
        if changed:
            # Pending PCM still belongs to local media. Never mix it into the
            # next connection's inference chunk or mark it as freshly captured.
            if self.on_pcm and self._raw_buf and self._run and self._run[0] == generation:
                self.on_pcm(bytes(self._raw_buf), self._run[1] + _ms(self._run[2]))
            self._reset_run()
        return changed

    def feed(self, data: bytes, *, end_ms: int | None = None,
             raw_pcm: bytes | None = None, aec: str = "off") -> int:
        """Audio just read from the device, ending now. Returns how many chunks went out."""
        mic = self.session.view.sensors["microphone"]
        connected = self.session.state is Conn.CONNECTED
        if not mic.enabled or (not connected and not self.local_recording_allowed()):
            self._reset_run()
            self.aec_status = "off"
            return 0
        self.connection_changed(connected, mic.generation)
        raw_pcm = data if raw_pcm is None else raw_pcm
        if len(data) != len(raw_pcm):
            raise ValueError("unaligned analysis and recording PCM")
        self.aec_status = aec
        if self._skip:
            dropped = min(self._skip, len(data))
            self._skip -= dropped
            raw_pcm = raw_pcm[dropped:]
            data = data[dropped:]  # what is left still ends now, so its timestamps hold
            if not data:
                return 0
        self.last_pcm = raw_pcm
        self.zero_ms = 0 if any(raw_pcm) else self.zero_ms + _ms(len(raw_pcm))
        now = self._clock() if end_ms is None else end_ms
        if self._run is not None:
            expected_end = self._run[1] + _ms(self._run[2] + len(self._buf) + len(data))
            if self._run[0] != mic.generation or abs(expected_end - now) > RESYNC_MS:
                self._reset_run()
        if self._run is None:
            self._run = [mic.generation, now - _ms(len(data)), 0]
        self._buf += data
        self._raw_buf += raw_pcm
        if data:
            self._statuses.append((len(data), aec))
        sent = 0
        while len(self._buf) >= self.chunk_bytes:
            chunk = bytes(self._buf[: self.chunk_bytes])
            del self._buf[: self.chunk_bytes]
            raw_chunk = bytes(self._raw_buf[: self.chunk_bytes])
            del self._raw_buf[: self.chunk_bytes]
            capture_ms = self._run[1] + _ms(self._run[2])
            self._run[2] += len(chunk)
            if connected and len(self.recording) < self._keep:
                self.recording += raw_chunk[: self._keep - len(self.recording)]
            if self.on_pcm:
                self.on_pcm(raw_chunk, capture_ms)
            states = set()
            remaining = self.chunk_bytes
            while remaining:
                count, state = self._statuses.popleft()
                take = min(count, remaining)
                states.add(state)
                remaining -= take
                if take < count:
                    self._statuses.appendleft((count - take, state))
            # A chunk spanning a reset must never advertise fully active AEC.
            chunk_aec = next(iter(states)) if len(states) == 1 else "uncancelled"
            if connected and self.session.send_audio(chunk, capture_ms, aec=chunk_aec):
                sent += 1
        self.sent += sent
        return sent


class AudioConverter:
    """Convert Qt PCM to mono PCM16, preserving resampler state across reads."""

    def __init__(self, fmt, target_rate=AUDIO_RATE) -> None:
        import numpy as np
        import soxr
        from PySide6.QtMultimedia import QAudioFormat

        formats = {
            QAudioFormat.SampleFormat.UInt8: ("u1", 128.0, 128.0),
            QAudioFormat.SampleFormat.Int16: ("<i2", 0.0, 32768.0),
            QAudioFormat.SampleFormat.Int32: ("<i4", 0.0, 2147483648.0),
            QAudioFormat.SampleFormat.Float: ("<f4", 0.0, 1.0),
        }
        if fmt.sampleFormat() not in formats or fmt.sampleRate() <= 0 or fmt.channelCount() <= 0:
            raise RuntimeError("unsupported microphone PCM format")
        self._np = np
        self._dtype, self._offset, self._scale = formats[fmt.sampleFormat()]
        self._channels = fmt.channelCount()
        self._frame_bytes = np.dtype(self._dtype).itemsize * self._channels
        self._pending = b""
        self._target_rate = target_rate
        self._resampler = (soxr.ResampleStream(fmt.sampleRate(), target_rate, 1, dtype="float32")
                           if fmt.sampleRate() != target_rate else None)

    @property
    def delay_ms(self) -> int:
        return round(self._resampler.delay() * 1000 / self._target_rate) if self._resampler else 0

    def convert(self, data: bytes) -> bytes:
        np = self._np
        data = self._pending + data
        n = len(data) // self._frame_bytes * self._frame_bytes
        self._pending = data[n:]
        if not n:
            return b""
        samples = np.frombuffer(data[:n], dtype=self._dtype).astype(np.float32)
        samples = (samples - self._offset) / self._scale
        mono = samples.reshape(-1, self._channels).mean(axis=1)
        mono = np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0)
        if self._resampler:
            mono = self._resampler.resample_chunk(mono)
        return np.clip(np.rint(mono * 32768.0), -32768, 32767).astype("<i2").tobytes()


class QtAudioInput:
    """Reads a Qt audio input in the contract's format and feeds a LiveMicrophone."""

    def __init__(self, mic: LiveMicrophone, device=None) -> None:
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

        device = device if device is not None else QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise RuntimeError("no microphone found")
        fmt = QAudioFormat()
        rate = 48_000 if mic.player is not None else AUDIO_RATE
        fmt.setSampleRate(rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
        if not device.isFormatSupported(fmt):
            raise RuntimeError(f"{device.description()} has no supported capture format")
        self.format = fmt
        self.rate = rate
        self.converter = AudioConverter(fmt, rate)
        self._capture_buffer = bytearray()
        self._capture_start_ms = None
        self._device_bytes = 0
        self._generation = None
        self._resampled_states = deque()
        self._raw_converter = self._clean_converter = None
        self._media_devices = QMediaDevices()
        if mic.player is not None:
            self._media_devices.audioOutputsChanged.connect(self._reset_echo)
        self.mic = mic
        self.device = device
        self.source = QAudioSource(device, fmt)
        self.source.setBufferSize(fmt.bytesForDuration(500_000))  # 0.5 s in device format
        self._io = None

    def _reset_echo(self):
        from PySide6.QtMultimedia import QAudioFormat

        self.converter = AudioConverter(self.format, self.rate)
        self._capture_buffer.clear()
        self._capture_start_ms = self._generation = None
        self._device_bytes = 0
        self._resampled_states.clear()
        if self.mic.player is not None:
            self.mic.player.reset_echo()
            fmt = QAudioFormat()
            fmt.setSampleRate(48_000)
            fmt.setChannelCount(1)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self._raw_converter = AudioConverter(fmt)
            self._clean_converter = AudioConverter(fmt)
            self.mic.aec_status = "reference_unavailable"
        self.mic._reset_run()

    def start(self) -> None:
        self._reset_echo()
        self._io = self.source.start()
        if self._io is None:
            raise RuntimeError(f"could not open {self.device.description()}")
        self.mic.device_started()
        self._io.readyRead.connect(self._read)

    def stop(self) -> None:
        if self._io is not None:
            self._io.readyRead.disconnect(self._read)
        self._io = None
        self.source.stop()
        self._reset_echo()
        self.mic.aec_status = "off"

    def _device_duration_ms(self, byte_count: int) -> int:
        # QAudioFormat.durationForBytes accepts only a signed 32-bit count.
        frames = byte_count // self.format.bytesPerFrame()
        return frames * 1000 // self.format.sampleRate()

    def _read(self) -> None:
        if self._io is not None:
            data = self._io.readAll().data()
            if data:
                mic = self.mic
                now = mic._clock()
                sensor = mic.session.view.sensors["microphone"]
                connected = mic.session.state is Conn.CONNECTED
                if not sensor.enabled or (not connected and not mic.local_recording_allowed()):
                    if self._generation is not None:
                        self._reset_echo()
                    return
                elapsed_ms = self._device_duration_ms(self._device_bytes + len(data))
                discontinuity = (self._capture_start_ms is not None and
                                 abs(self._capture_start_ms + elapsed_ms - now) > RESYNC_MS)
                if (mic.connection_changed(connected, sensor.generation) or
                        self._generation != sensor.generation or discontinuity):
                    self._reset_echo()
                    elapsed_ms = self._device_duration_ms(len(data))
                self._generation = sensor.generation
                if self._capture_start_ms is None:
                    self._capture_start_ms = now - elapsed_ms
                self._device_bytes += len(data)
                pcm = self.converter.convert(bytes(data))
                # Qt callback timing may jitter even when device samples are contiguous.
                end_ms = self._capture_start_ms + elapsed_ms - self.converter.delay_ms
                if self.mic.player is None:
                    if pcm:
                        mic.feed(pcm, end_ms=end_ms)
                    return
                self._capture_buffer.extend(pcm)
                size = len(self._capture_buffer) // 960 * 960  # complete 10 ms blocks at 48 kHz
                if not size:
                    return
                raw = bytes(self._capture_buffer[:size])
                del self._capture_buffer[:size]
                end_ms -= len(self._capture_buffer) // 96
                cleaned, status = mic.player.cancel_echo(raw, end_ms - size // 96) if connected else (raw, "off")
                self._feed_resampled(raw, cleaned, status, end_ms)

    def _feed_resampled(self, raw: bytes, cleaned: bytes, status: str, end_ms: int) -> None:
        # 48 kHz input arrives in 10 ms blocks. The resampler sometimes holds
        # multiple blocks and emits them together, even across an AEC change.
        self._resampled_states.append((len(raw) // 3, status))
        audio = self._clean_converter.convert(cleaned)
        original = self._raw_converter.convert(raw)
        if len(audio) != len(original):
            raise RuntimeError("raw and filtered resamplers produced different sample counts")
        output_end = end_ms - self._clean_converter.delay_ms
        offset = 0
        while offset < len(audio):
            count, state = self._resampled_states.popleft()
            stop = min(offset + count, len(audio))
            if stop - offset < count:
                self._resampled_states.appendleft((count - (stop - offset), state))
            self.mic.feed(audio[offset:stop], raw_pcm=original[offset:stop], aec=state,
                          end_ms=output_end - _ms(len(audio) - stop))
            offset = stop


def _ms(n_bytes: int) -> int:
    return n_bytes // 2 * 1000 // AUDIO_RATE
