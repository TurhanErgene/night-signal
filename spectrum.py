"""Measure 12 logarithmic bands of stereo PCM with a Hann-windowed FFT."""

from cmath import exp
from math import cos, log10, tau


BAND_EDGES = (20, 36, 63, 112, 200, 355, 630, 1120, 2000, 3550, 6300, 11200, 20000)
FRAME_COUNT = 4096


class Spectrum:
    def __init__(self, sample_rate):
        bits = FRAME_COUNT.bit_length() - 1
        self.order = [int(f"{index:0{bits}b}"[::-1], 2) for index in range(FRAME_COUNT)]
        window = [(1 - cos(tau * index / FRAME_COUNT)) / 2 for index in range(FRAME_COUNT)]
        self.window = [window[index] / 32768 for index in self.order]
        self.reference = sum(window) ** 2 * 1.5
        self.stages = []
        for bit in range(bits):
            width = 2 << bit
            self.stages.append([exp(-1j * tau * index / width) for index in range(width // 2)])
        self.bands = [
            range((start * FRAME_COUNT + sample_rate - 1) // sample_rate,
                  (end * FRAME_COUNT + sample_rate - 1) // sample_rate)
            for start, end in zip(BAND_EDGES, BAND_EDGES[1:])
        ]
        self.levels = (0.0,) * len(self.bands)
        self.peaks = self.levels
        self.peak_holds = [0] * len(self.bands)

    def measure(self, samples):
        # Pack left and right into one FFT. Summing the power of +k and -k
        # preserves both channels, including audio with opposite stereo phases.
        values = [
            complex(samples[2 * index], samples[2 * index + 1]) * weight
            for index, weight in zip(self.order, self.window)
        ]
        for weights in self.stages:
            half = len(weights)
            for start in range(0, FRAME_COUNT, 2 * half):
                for offset, weight in enumerate(weights):
                    lower = start + offset
                    upper = lower + half
                    odd = values[upper] * weight
                    even = values[lower]
                    values[lower] = even + odd
                    values[upper] = even - odd

        levels = []
        for bins, previous in zip(self.bands, self.levels):
            power = sum(abs(values[index]) ** 2 + abs(values[-index]) ** 2 for index in bins)
            decibels = 10 * log10(max(power / self.reference, 1e-12))
            level = min(1.0, (decibels + 60) / 60)
            levels.append(max(0.0, level, previous - 0.20))
        self.levels = tuple(levels)
        peaks = []
        for index, (level, previous) in enumerate(zip(self.levels, self.peaks)):
            if level >= previous:
                peaks.append(level)
                self.peak_holds[index] = 8
            elif self.peak_holds[index]:
                peaks.append(previous)
                self.peak_holds[index] -= 1
            else:
                peaks.append(max(level, previous - 0.08))
        self.peaks = tuple(peaks)
        return self.levels
