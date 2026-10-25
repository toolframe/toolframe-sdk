"""SDK 0.4 baseline consumer; keep this source unchanged for compatibility tests."""

from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Plugin,
    PluginDefinition,
    ScopeSpec,
    ServiceSpec,
)


class Echo(Plugin):
    async def invoke(self, service, method, params):
        return {"message": params["message"]}


plugin = PluginDefinition(
    id="compat-echo",
    factory="sdk04_plugin:Echo",
    authorization=AuthorizationSpec(
        scopes={"compat-echo.read": ScopeSpec(title="Read echo")}
    ),
    services={
        "compat-echo.messages": ServiceSpec(
            methods={
                "echo": MethodSpec(
                    audience="business",
                    required_scopes=("compat-echo.read",),
                    public_api=True,
                    read_only=True,
                    input_schema={
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                )
            }
        )
    },
)
