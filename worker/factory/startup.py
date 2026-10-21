"""Trusted operator startup hook, executed before orchestration registration."""

import logging
import os
import signal
import subprocess
import threading

logger = logging.getLogger("issue-worker.startup")


def run_startup_script(settings):
    if not settings.startup_script.strip():
        return
    from factory.adapters.control import redact

    logger.info("Worker profile startup script started")
    process = subprocess.Popen(
        ["/bin/sh", "-eu", "-c", settings.startup_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Drain output continuously but retain at most 64 KiB; script text and raw
    # credentials never enter logs. No unbounded in-memory communicate() buffer.
    output = bytearray()

    def drain():
        while chunk := process.stdout.read(4096):
            output.extend(chunk[: max(0, 65536 - len(output))])

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=settings.startup_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # Clean up background children as well as a timed-out parent.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        reader.join(timeout=5)
        process.stdout.close()
    if output:
        logger.info(
            "Startup script output (up to 64 KiB):\n%s",
            redact(output.decode(errors="replace")),
        )
    if timed_out:
        logger.error("Worker profile startup script timed out")
        raise RuntimeError("Worker startup script timed out; task registration aborted")
    if process.returncode:
        logger.error(
            "Worker profile startup script failed with exit status %s",
            process.returncode,
        )
        raise RuntimeError(
            f"Worker startup script failed with exit status {process.returncode}; task registration aborted"
        )
    logger.info("Worker profile startup script completed")
