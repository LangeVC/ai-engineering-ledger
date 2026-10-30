from __future__ import annotations

from pathlib import Path


APP_JS = Path("ael/assets/app.js")


def test_alias_cards_expose_edit_without_replacing_delete() -> None:
    source = APP_JS.read_text()

    assert 'class="button ghost alias-edit"' in source
    assert 'class="button ghost alias-delete"' in source
    assert "editAlias(b.dataset.id)" in source
    assert "deleteAlias(b.dataset.id)" in source


def test_edit_mode_populates_fields_and_locks_alias_identity() -> None:
    source = APP_JS.read_text()

    assert "editingAliasId=id" in source
    assert "aliasForm.elements.id.value=alias.id" in source
    assert "aliasForm.elements.id.readOnly=true" in source
    assert 'aliasForm.elements.secret_ref.value=alias.secret_ref||""' in source
    assert 'if(editingAliasId&&id!==editingAliasId)throw new Error("Alias id cannot be changed while editing")' in source


def test_cancel_and_success_restore_create_mode_but_failure_preserves_form() -> None:
    source = APP_JS.read_text()
    submit = next(line for line in source.splitlines() if line.startswith('$("#alias-form").addEventListener("submit"'))

    assert 'aliasCancel.textContent="Cancel edit"' in source
    assert "aliasCancel.addEventListener(\"click\",cancelAliasEdit)" in source
    assert "aliasForm.reset()" in source
    assert "aliasForm.elements.id.readOnly=false" in source
    assert submit.index('await api("/api/config/aliases"') < submit.index("form.reset()") < submit.index("finishAliasEdit()") < submit.index("catch(error)")
    assert "reset" not in submit[submit.index("catch(error)") :]


def test_delete_semantics_and_secret_reference_contract_remain_intact() -> None:
    source = APP_JS.read_text()

    assert 'prompt(`Type ${id} to remove this alias. Ledger observations remain.`)' in source
    assert 'api("/api/config/aliases/delete"' in source
    assert 'secret_ref:f.get("secret_ref")||null' in source
