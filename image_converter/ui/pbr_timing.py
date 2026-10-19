from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter

from PyQt6 import sip
from PyQt6.QtOpenGL import QOpenGLTimerQuery


@dataclass(frozen=True, slots=True)
class PbrPreviewTiming:
    material_serial: int
    texture_size: tuple[int, int]
    upload_cpu_ms: float | None = None
    uploaded_maps: tuple[str, ...] = ()
    draw_cpu_ms: float | None = None
    draw_gpu_ms: float | None = None
    viewport_size: tuple[int, int] | None = None
    gpu_status: str = "Not sampled"


class GpuDrawTimer:
    """One bounded, non-blocking query; all calls require the owning GL context.

    No query result is read before availability. Tokens identify the material
    and draw sample so a late result cannot be attached to another scene/frame.
    """

    TIMEOUT_SECONDS = 2.0

    def __init__(
        self, *, query_factory: Callable = QOpenGLTimerQuery,
        dispose_query: Callable = sip.delete, clock=perf_counter,
    ):
        self._query_factory = query_factory
        self._dispose_query = dispose_query
        self._clock = clock
        self._query = None
        self._active_token = None
        self._pending = None
        self.supported = False
        self.status = "GPU timer unavailable"

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def begin(self, token: object) -> bool:
        if not self.supported or self.pending or self._active_token is not None:
            return False
        try:
            if self._query is None:
                self._query = self._query_factory()
                if not self._query.create():
                    self._disable("GPU timer creation failed")
                    return False
            self._query.begin()
            self._active_token = token
            return True
        except Exception:
            self._disable("GPU timer unavailable")
            return False

    def end(self) -> None:
        if self._active_token is None:
            return
        token = self._active_token
        self._active_token = None
        try:
            self._query.end()
            self._pending = (token, self._clock())
            self.status = "GPU result pending"
        except Exception:
            self._disable("GPU timer failed")

    def poll(self) -> tuple[object, float] | None:
        if self._pending is None:
            return None
        token, started = self._pending
        try:
            if self._query.isResultAvailable():
                elapsed_ns = self._query.waitForResult()
                self._pending = None
                self.status = "GPU timer ready"
                return token, max(0, elapsed_ns) / 1_000_000
            if self._clock() - started >= self.TIMEOUT_SECONDS:
                self._disable("GPU timer timed out")
        except Exception:
            self._disable("GPU timer failed")
        return None

    def _disable(self, reason: str) -> None:
        self.release()
        self.supported = False
        self.status = reason

    def release(self) -> None:
        self._pending = None
        self._active_token = None
        if self._query is not None:
            query = self._query
            self._query = None
            # Delete exactly once while the owning context is current. In Qt
            # 6.11, destroy() clears the saved context, which the C++ destructor
            # can then dereference. The destructor itself frees the GL query ID.
            self._dispose_query(query)


def supports_gpu_timer(context) -> bool:
    if context is None or context.isOpenGLES():
        return False
    version = context.format()
    return (
        (version.majorVersion(), version.minorVersion()) >= (3, 3)
        or context.hasExtension(b"GL_ARB_timer_query")
        or context.hasExtension(b"GL_EXT_timer_query")
    )
