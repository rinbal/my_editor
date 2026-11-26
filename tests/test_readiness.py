"""The Publication Check core (nostr/readiness.py): the cases STANDUP runs
too, the vendored copies, and what MyEditor adds."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nostr import readiness
from nostr.readiness import BLOCKER, STANDUP, evaluate

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "tests" / "fixtures" / "publication_check_cases.json").read_text(encoding="utf-8"))
# Copied unchanged from STANDUP (see tests/fixtures/README.md); re-copy, never edit.
RULES_SHA256 = "adc5d7c51e35e75762d92ecfe63e05b26cda2e3ea243f3bde026972b3dcda2df"
CASES_SHA256 = "ae8e03561bea3d785161d2f93d552e54f493b82ec1c570fafedff01e95ef9675"


def _content(value):
    if isinstance(value, str):
        return value
    return "".join(piece if isinstance(piece, str) else piece["repeat"] * piece["times"] for piece in value)


def _article(tags, content="Text."):
    return {"kind": 30023, "created_at": CASES["now"], "tags": tags, "content": content}


def test_the_vendored_rules_and_cases_are_standups_own():
    assert hashlib.sha256((ROOT / "nostr" / "publication_check.json").read_bytes()).hexdigest() == RULES_SHA256
    assert hashlib.sha256((ROOT / "tests" / "fixtures" / "publication_check_cases.json").read_bytes()).hexdigest() == CASES_SHA256


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_the_shared_cases_get_standups_verdict(case):
    event = _article(case["event"]["tags"], _content(case["event"]["content"]))
    result = evaluate(event, outcome=case["outcome"], now=CASES["now"], own_checks=False,
                      **case.get("facts", {}))
    found = [{"code": f.code, "severity": f.severity, **({"params": dict(f.params)} if f.params else {})}
             for f in result.findings]
    assert (result.ready, found, list(result.checked)) == (case["ready"], case["findings"], case["checked"])


def test_a_secret_key_or_no_link_name_blocks_first():
    from tests.test_secret_keys import NSEC
    tags = [["title", "A long enough title"], ["summary", "S"], ["image", "https://x.org/a.jpg"], ["t", "bitcoin"]]
    result = evaluate(_article(tags, "My key: " + NSEC), now=CASES["now"])
    assert [(f.code, f.severity) for f in result.findings[:2]] == [
        (readiness.SECRET_KEY, BLOCKER), (readiness.NO_LINK_NAME, BLOCKER)]
    assert evaluate(_article([["d", "a-read"], *tags]), now=CASES["now"]).ready


def test_myeditors_stricter_cover_rule_says_what_it_would_drop_in_its_own_words():
    """An address the shared rule allows and the publisher's refuses (one
    on this network only) is MyEditor's cover.local, not "use https"."""
    tags = [["d", "a"], ["title", "A long enough title"], ["image", "http://old-blog.example.org/c.jpg"]]
    assert "cover.unsafe" not in [f.code for f in evaluate(_article(tags), now=1).findings]
    strict = evaluate(_article(tags), now=1, cover_ok=lambda address: address.startswith("https://"))
    assert [f.code for f in strict.blockers] == [readiness.COVER_LOCAL]
    unsafe = evaluate(_article([["d", "a"], ["title", "T"], ["image", "data:image/png;base64,AA"]]), now=1)
    assert [f.code for f in unsafe.blockers] == ["cover.unsafe"]


def test_the_words_follow_the_interface_language():
    result = evaluate(_article([["d", "a"]], ""), outcome=STANDUP, now=1)
    assert readiness.headline(result, lang="de") == "2 Punkte zu beheben"
    assert readiness.headline(evaluate(_article([["d", "a"], ["title", "T"]]), now=1), lang="en") == "Ready to Publish"
    assert readiness.suggestions_line(result, lang="en") == "3 suggestions"
    title_missing = result.findings[0]
    assert readiness.finding_sentence(title_missing, lang="de") == readiness.RULES["messages"]["de"]["title.missing"]
    assert readiness.group_label("tags", lang="en") == "Hashtags"


def test_the_check_loads_without_qt():
    code = "import sys; import nostr.readiness; print(any(m.startswith('PySide6') for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
    assert out.stdout.strip() == "False"
