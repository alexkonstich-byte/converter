"""Every UI string must have an English translation; English is the default."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app  # noqa: E402
import converter  # noqa: E402
import dialogs  # noqa: E402
import i18n  # noqa: E402
import queue_logic  # noqa: E402
import themes  # noqa: E402
import ui_kit  # noqa: E402

CYR = re.compile("[А-Яа-яЁё]")


def _tr_literals():
    for f in ROOT.glob("*.py"):
        if f.name == "i18n.py":
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                yield f.name, node.args[0].value


def _module_constants():
    for p in converter.PRESETS:
        yield p.label
        yield p.description
    yield from converter.NAME_TOKENS.values()
    yield from (h for _, h in converter._ERROR_HINTS)
    yield from converter.AUDIO_CODEC_LABELS.values()
    for keys, desc in dialogs.HOTKEYS:
        yield keys
        yield desc
    yield from ui_kit.KIND_LABEL.values()
    yield from (t.name for t in themes.THEMES)
    yield from app.GROUP_LABEL.values()


def test_default_language_is_english():
    assert i18n.DEFAULT_LANGUAGE == "en"


def test_every_tr_string_is_translated():
    missing = [(f, s) for f, s in _tr_literals() if s not in i18n.EN]
    assert not missing, missing


def test_module_constants_are_translated():
    missing = [s for s in _module_constants() if CYR.search(s) and s not in i18n.EN]
    assert not missing, missing


def test_no_untranslated_cyrillic_literals_in_ui():
    """Cyrillic string literals outside tr() would leak Russian into the English UI."""
    leaks = []
    for f in ROOT.glob("*.py"):
        if f.name in ("i18n.py",):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        wrapped = {id(n.args[0]) for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "tr" and n.args}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and CYR.search(node.value) and id(node) not in wrapped:
                leaks.append((f.name, node.lineno, node.value[:40]))
    allowed = set(_module_constants()) | {"файл", "файла", "файлов", "Файл"}
    leaks = [x for x in leaks if not any(x[2] in a for a in allowed)]
    assert not leaks, leaks


def test_formatting_and_fallback():
    i18n.set_language("en")
    assert i18n.tr("Готово {0} из {1}", 2, 3) == "Done 2 of 3"
    assert i18n.tr("непереведённая строка") == "непереведённая строка"
    i18n.set_language("ru")
    assert i18n.tr("Готово {0} из {1}", 2, 3) == "Готово 2 из 3"
    i18n.set_language("en")


def test_english_plural():
    i18n.set_language("en")
    assert [queue_logic.files_word(n) for n in (1, 2, 21)] == ["file", "files", "files"]
