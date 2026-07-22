from __future__ import annotations

from pathlib import Path


def test_documented_service_protocol_definition_is_executable() -> None:
    usage = Path(__file__).resolve().parents[1] / "docs" / "usage.md"
    source = usage.read_text()
    section = source.split("## Service Protocol Definition", 1)[1]
    code = section.split("```python", 1)[1].split("```", 1)[0]
    namespace: dict[str, object] = {}

    exec(compile(code, str(usage), "exec"), namespace)  # noqa: S102

    protocol = namespace["EXAMPLE_PROTOCOL"]
    assert tuple(protocol.operations) == ("presence.report",)
    assert tuple(protocol.messages) == ("presence.report",)
    assert protocol.view_families["status"].writer == "service"

    payload = namespace["example_advertisement_payload"](
        service_id="presence-main",
        session_id="session-1",
    )
    assert payload["serviceId"] == "presence-main"
    assert payload["supportedOperations"] == ["presence.report"]
