import json

from flask import Blueprint, Response
from engine.orchestrator import sse_attach, sse_detach
from logging_.engine_logger import get_engine_logger

log = get_engine_logger()

bp = Blueprint("sse", __name__, url_prefix="/events")


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@bp.get("/<run_id>")
def events(run_id: str):
    q = sse_attach(run_id)

    if q is None:
        # Not an error worth a 404: the client asked for a stream, so answer in
        # the stream's own protocol and let it close. Two causes, see
        # engine/orchestrator.py::sse_attach - another worker holds the run, or
        # the run is already gone. Previously this returned an empty stream that
        # hung until the client gave up, which looked like a stalled run.
        log.warning("[%s] No SSE queue in this worker; closing the stream immediately.", run_id)
        return Response(
            _event({
                "type": "stream_unavailable",
                "run_id": run_id,
                "reason": "This worker is not serving that run, or the run has already finished "
                          "and its stream was read. Results are written to ./logs either way.",
            }),
            mimetype="text/event-stream",
        )

    log.info("[%s] Client connected to SSE stream.", run_id)

    def stream():
        try:
            while True:
                msg = q.get()

                if msg is None:
                    log.debug("[%s] Received None sentinel, closing SSE stream.", run_id)
                    sse_detach(run_id)
                    break

                yield f"data: {msg}\n\n"
        except GeneratorExit:
            # The client went away mid-run (tab closed, reload). The queue stays:
            # EventSource reconnects on its own and has to find its events still
            # there. _sweep_abandoned_runs collects it if nobody comes back.
            log.info("[%s] SSE client disconnected before the run finished.", run_id)
            raise

    return Response(stream(), mimetype="text/event-stream")
