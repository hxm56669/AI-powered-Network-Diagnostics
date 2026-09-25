"""Bounded cooperative shutdown with a process deadline for stalled native code."""
import os
import threading

from .providers.embedding import kill_model_workers


class ShutdownDeadline:
    def __init__(self, seconds, hard_exit=os._exit):
        self.hard_exit = hard_exit
        self.timer = threading.Timer(seconds, self.expired)
        self.timer.daemon = True

    def expired(self):
        # Kill stalled model workers before exiting. Session data lives only in RAM.
        kill_model_workers()
        os.write(2, b'{"event":"shutdown_deadline_exceeded","session_data":"discarded"}\n')
        self.hard_exit(124)

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *args):
        self.timer.cancel()


def run_server(app, settings):
    """Start the deadline at SIGTERM, including HTTP draining and executor teardown."""
    import uvicorn

    class BoundedServer(uvicorn.Server):
        deadline = None

        def handle_exit(self, sig, frame):
            if self.deadline is None:
                self.deadline = ShutdownDeadline(settings.service.shutdown_timeout_s)
                self.deadline.__enter__()
            super().handle_exit(sig, frame)

    server = BoundedServer(uvicorn.Config(app, host=settings.service.host, port=settings.service.port,
        workers=1, log_level="info", timeout_graceful_shutdown=min(1, settings.service.shutdown_timeout_s / 2)))
    try:
        server.run()
    finally:
        if server.deadline is not None:
            server.deadline.__exit__(None, None, None)
