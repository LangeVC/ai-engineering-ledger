from __future__ import annotations

from pathlib import Path


APP_JS = Path("ael/assets/app.js")


def _handler(source: str, form_id: str) -> str:
    prefix = f'$("#{form_id}").addEventListener("submit",async event=>{{'
    line = next((line for line in source.splitlines() if line.startswith(prefix)), None)
    assert line is not None, f"missing submit handler for {form_id}"
    return line.removeprefix(prefix)


def test_alias_handler_caches_form_before_await_and_resets_only_after_success() -> None:
    body = _handler(APP_JS.read_text(), "alias-form")

    assert body.startswith("event.preventDefault();const form=event.currentTarget;try{")
    assert "new FormData(form)" in body
    assert "event.currentTarget" not in body.removeprefix(
        "event.preventDefault();const form=event.currentTarget;"
    )
    request = body.index('await api("/api/config/aliases"')
    reset = body.index("form.reset()")
    catch = body.index("catch(error)")
    assert request < reset < catch
    assert "reset" not in body[catch:]


def test_manual_snapshot_handler_never_dereferences_current_target_after_await() -> None:
    body = _handler(APP_JS.read_text(), "manual-form")

    prefix = "event.preventDefault();const form=event.currentTarget;"
    assert body.startswith(prefix)
    assert "new FormData(form)" in body
    assert "form.elements.product.value" in body
    assert "event.currentTarget" not in body.removeprefix(prefix)
