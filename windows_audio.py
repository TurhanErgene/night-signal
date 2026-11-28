"""Windows audio output and decoder lifetime management."""

import ctypes as ct
from ctypes import wintypes as win


class WaveFormat(ct.Structure):
    _fields_ = [
        ("format", win.WORD), ("channels", win.WORD), ("sample_rate", win.DWORD),
        ("bytes_per_second", win.DWORD), ("frame_bytes", win.WORD),
        ("bits_per_sample", win.WORD), ("extra_bytes", win.WORD),
    ]


class WaveHeader(ct.Structure):
    _fields_ = [
        ("data", ct.c_void_p), ("length", win.DWORD), ("recorded", win.DWORD),
        ("user", ct.c_size_t), ("flags", win.DWORD), ("loops", win.DWORD),
        ("next", ct.c_void_p), ("reserved", ct.c_size_t),
    ]


class BasicJobLimits(ct.Structure):
    _fields_ = [
        ("process_time", ct.c_int64), ("job_time", ct.c_int64), ("flags", win.DWORD),
        ("min_working_set", ct.c_size_t), ("max_working_set", ct.c_size_t),
        ("process_limit", win.DWORD), ("affinity", ct.c_size_t),
        ("priority", win.DWORD), ("scheduling", win.DWORD),
    ]


class JobLimits(ct.Structure):
    _fields_ = [
        ("basic", BasicJobLimits), ("io_counters", ct.c_uint64 * 6),
        ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
        ("peak_process_memory", ct.c_size_t), ("peak_job_memory", ct.c_size_t),
    ]


def bind(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


kernel = ct.WinDLL("kernel32", use_last_error=True)
audio = ct.WinDLL("winmm")
create_event = bind(kernel, "CreateEventW", win.HANDLE, ct.c_void_p, win.BOOL, win.BOOL, win.LPCWSTR)
wait = bind(kernel, "WaitForSingleObject", win.DWORD, win.HANDLE, win.DWORD)
close_handle = bind(kernel, "CloseHandle", win.BOOL, win.HANDLE)
create_job = bind(kernel, "CreateJobObjectW", win.HANDLE, ct.c_void_p, win.LPCWSTR)
set_job_information = bind(
    kernel, "SetInformationJobObject", win.BOOL, win.HANDLE, ct.c_int, ct.c_void_p, win.DWORD,
)
open_process = bind(kernel, "OpenProcess", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
assign_job = bind(kernel, "AssignProcessToJobObject", win.BOOL, win.HANDLE, win.HANDLE)
wave_open = bind(audio, "waveOutOpen", win.UINT, ct.POINTER(win.HANDLE), win.UINT,
                 ct.POINTER(WaveFormat), ct.c_size_t, ct.c_size_t, win.DWORD)
wave_prepare = bind(audio, "waveOutPrepareHeader", win.UINT, win.HANDLE, ct.POINTER(WaveHeader), win.UINT)
wave_unprepare = bind(audio, "waveOutUnprepareHeader", win.UINT, win.HANDLE, ct.POINTER(WaveHeader), win.UINT)
wave_write = bind(audio, "waveOutWrite", win.UINT, win.HANDLE, ct.POINTER(WaveHeader), win.UINT)
wave_pause = bind(audio, "waveOutPause", win.UINT, win.HANDLE)
wave_restart = bind(audio, "waveOutRestart", win.UINT, win.HANDLE)
wave_reset = bind(audio, "waveOutReset", win.UINT, win.HANDLE)
wave_close = bind(audio, "waveOutClose", win.UINT, win.HANDLE)
wave_get_volume = bind(audio, "waveOutGetVolume", win.UINT, win.HANDLE, ct.POINTER(win.DWORD))
wave_set_volume = bind(audio, "waveOutSetVolume", win.UINT, win.HANDLE, win.DWORD)
wave_error_text = bind(audio, "waveOutGetErrorTextW", win.UINT, win.UINT, win.LPWSTR, win.UINT)

WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_EVENT = 0x50000
BUFFER_QUEUED = 0x10
BUFFER_COUNT = 8
WAIT_FAILED = 0xFFFFFFFF
JOB_EXTENDED_LIMITS = 9
JOB_KILL_ON_CLOSE = 0x2000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


def check_audio(result):
    if result:
        message = ct.create_unicode_buffer(256)
        wave_error_text(result, message, len(message))
        raise OSError(message.value or f"Windows audio error {result}.")


class WindowsAudio:
    def __init__(self, sample_rate, block_bytes):
        self.event = create_event(None, False, False, None)
        if not self.event:
            raise ct.WinError(ct.get_last_error())
        self.device = win.HANDLE()
        self.buffers = []
        self.index = 0
        self.prefill = BUFFER_COUNT
        self.volume_before_mute = None
        self.header_size = ct.sizeof(WaveHeader)

        try:
            pcm = WaveFormat(1, 2, sample_rate, sample_rate * 4, 4, 16, 0)
            check_audio(wave_open(ct.byref(self.device), WAVE_MAPPER, ct.byref(pcm),
                                  self.event, 0, CALLBACK_EVENT))
            # About 186 ms at 44.1 kHz absorbs decoder and scheduling stalls.
            check_audio(wave_pause(self.device))
            for _ in range(BUFFER_COUNT):
                storage = ct.create_string_buffer(block_bytes)
                header = WaveHeader(ct.addressof(storage), block_bytes)
                check_audio(wave_prepare(self.device, ct.byref(header), self.header_size))
                self.buffers.append((storage, header))
        except OSError:
            self.close()
            raise

    def wait_for_slot(self, stop_requested):
        return self.wait_for_buffer(self.buffers[self.index][1], stop_requested)

    def wait_for_buffer(self, header, stop_requested):
        while header.flags & BUFFER_QUEUED and not stop_requested.is_set():
            if wait(self.event, 25) == WAIT_FAILED:
                raise ct.WinError(ct.get_last_error())
        return not stop_requested.is_set()

    def write(self, block):
        if not self.prefill and not any(header.flags & BUFFER_QUEUED for _, header in self.buffers):
            check_audio(wave_pause(self.device))
            self.prefill = len(self.buffers)
        _, header = self.buffers[self.index]
        ct.memmove(header.data, block, len(block))
        header.length = len(block)
        check_audio(wave_write(self.device, ct.byref(header), self.header_size))
        self.index = (self.index + 1) % len(self.buffers)
        if self.prefill:
            self.prefill -= 1
            if not self.prefill:
                check_audio(wave_restart(self.device))

    def drain(self, stop_requested):
        if self.prefill and not stop_requested.is_set():
            check_audio(wave_restart(self.device))
        for _, header in self.buffers:
            if not self.wait_for_buffer(header, stop_requested):
                break

    def set_muted(self, muted):
        if muted and self.volume_before_mute is None:
            volume = win.DWORD()
            if wave_get_volume(self.device, ct.byref(volume)) or wave_set_volume(self.device, 0):
                return False
            self.volume_before_mute = volume.value
        elif not muted and self.volume_before_mute is not None:
            check_audio(wave_set_volume(self.device, self.volume_before_mute))
            self.volume_before_mute = None
        return True

    def close(self):
        if self.device:
            wave_reset(self.device)
            if self.volume_before_mute is not None:
                wave_set_volume(self.device, self.volume_before_mute)
            for _, header in self.buffers:
                wave_unprepare(self.device, ct.byref(header), self.header_size)
            wave_close(self.device)
            self.device = None
        if self.event:
            close_handle(self.event)
            self.event = None


class DecoderJob:
    def __init__(self):
        self.handle = create_job(None, None)
        if not self.handle:
            raise ct.WinError(ct.get_last_error())
        limits = JobLimits()
        limits.basic.flags = JOB_KILL_ON_CLOSE
        if not set_job_information(self.handle, JOB_EXTENDED_LIMITS, ct.byref(limits), ct.sizeof(limits)):
            error = ct.WinError(ct.get_last_error())
            self.close()
            raise error

    def add(self, pid):
        process = open_process(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise ct.WinError(ct.get_last_error())
        try:
            if not assign_job(self.handle, process):
                raise ct.WinError(ct.get_last_error())
        finally:
            close_handle(process)

    def close(self):
        if self.handle:
            close_handle(self.handle)
            self.handle = None
