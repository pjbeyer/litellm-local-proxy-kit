"""Profile-local adapter for the portable proxy lifecycle matcher."""
from pathlib import Path


def register(ctx):
    # Deferred import keeps missing/broken source diagnostic at registration.
    from .guard import evaluate

    policy = Path(__file__).with_name("config.json")

    def before_tool(tool_name="", args=None, **_):
        return evaluate(tool_name, args, policy)

    ctx.register_hook("pre_tool_call", before_tool)
