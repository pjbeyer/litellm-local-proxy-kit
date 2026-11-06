"""Profile-local adapter for the portable proxy lifecycle matcher."""
from pathlib import Path

# Fixed, non-secret telemetry-lifecycle diagnostic. Written only during
# registration (outside any tool callback) and never inside a callback;
# no exception detail, secret, or path is ever recorded. States:
#   cleanup_unregistered -> register() has not completed yet
#   cleanup_registered   -> bounded on_unload registered, worker start attempted
#   cleanup_unsupported  -> host on_unload API absent/noncallable: no worker
#   cleanup_failed       -> on_unload registration raised: no worker
#   start_failed         -> sink_start raised despite registration: no worker
SINK_LIFECYCLE_STATE = "cleanup_unregistered"


def register(ctx):
    # Deferred import keeps missing/broken source diagnostic at registration.
    # Worker lifecycle helpers arrive through the same guarded import.
    from .guard import evaluate, sink_shutdown, sink_start

    policy = Path(__file__).with_name("config.json")

    def before_tool(tool_name="", args=None, **_):
        return evaluate(tool_name, args, policy)

    ctx.register_hook("pre_tool_call", before_tool)

    # Decision-sink lifecycle (contract D5): bounded cleanup is registered
    # BEFORE the start, through the host context's actual on_unload API
    # (getattr on the real ctx — no assumed interface). A worker is
    # started only when cleanup registration succeeded. Every failure
    # path is contained here and latched into SINK_LIFECYCLE_STATE (a
    # fixed non-secret string) — it never escapes registration, never
    # removes the hook, and never changes any enforcement decision.
    global SINK_LIFECYCLE_STATE

    def stop_sink():
        sink_shutdown(5.0)

    covered = False
    try:
        unload = getattr(ctx, "on_unload", None)
        if callable(unload):
            unload(stop_sink)
            covered = True
            SINK_LIFECYCLE_STATE = "cleanup_registered"
        else:
            SINK_LIFECYCLE_STATE = "cleanup_unsupported"
    except Exception:
        SINK_LIFECYCLE_STATE = "cleanup_failed"
    if covered:
        try:
            sink_start()
        except Exception:
            SINK_LIFECYCLE_STATE = "start_failed"