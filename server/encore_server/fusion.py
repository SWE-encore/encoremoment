"""Capture-time fusion for laughter. One observation per disjoint audio hop."""
from collections import deque
from dataclasses import dataclass
from statistics import median

from .audio import HOP_MS


@dataclass(frozen=True)
class TabRules:
    strong_audio: float = .25
    assisted_audio: float = .10
    smile: float = .20
    release_ratio: float = .5
    quiet_ms: int = 3000
    window_ms: int = 5000
    equivalent_hops: int = 3

    def holds(self, audio, visual):
        return audio >= self.strong_audio * self.release_ratio or (
            visual is not None and visual >= self.smile * self.release_ratio
            and audio >= self.assisted_audio * self.release_ratio)


def visual_for_hop(observations, center_ms):
    # The media clock belongs to the PC. Never give a face to two overlapping YAMNet windows.
    start = center_ms - HOP_MS // 2
    values = [score for t, score in observations if start <= t < start + HOP_MS and score is not None]
    return median(values) if values else None


class RollingFusion:
    def __init__(self, rules):
        self.rules = rules
        self.reset()

    def reset(self):
        self.samples = deque()
        self.last = None
        self.total = 0.0

    def push(self, t, audio, visual):
        if self.last is not None:
            if t <= self.last:
                return False
            if t - self.last != HOP_MS:
                self.reset()
        self.last = t
        rules = self.rules
        cutoff = t - rules.window_ms
        while self.samples and self.samples[0][0] <= cutoff:
            self.samples.popleft()
        # Per-channel saturation and a floor keep one loud or expressive hop
        # from outweighing several independent observations of both signals.
        paired = (visual is not None and audio >= rules.assisted_audio * rules.release_ratio
                  and visual >= rules.smile * rules.release_ratio)
        if paired:
            strength = (min(1, audio / rules.assisted_audio) + min(1, visual / rules.smile)) / 2
            self.samples.append((t, strength * HOP_MS / 1000))
        # Five seconds is not an integer number of hops. Only the overlapping
        # fraction of the oldest hop belongs to the integration interval.
        self.total = sum(value * min(1.0, (stamp - cutoff) / HOP_MS)
                         for stamp, value in self.samples)
        target = rules.equivalent_hops * HOP_MS / 1000
        return audio >= rules.strong_audio or (paired and self.total >= target - 1e-9)
