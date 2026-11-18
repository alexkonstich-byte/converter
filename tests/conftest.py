"""Engine tests assert on Russian messages; run them with the Russian UI language."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import i18n  # noqa: E402


@pytest.fixture(autouse=True)
def _russian(request):
    if request.module.__name__.endswith("test_i18n"):
        yield
        return
    i18n.set_language("ru")
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)
