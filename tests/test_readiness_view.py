"""The Publication Check on screen: the Publish sheet, Article Details and
the page's line (nostr/ui/readiness_view.py)."""
from PySide6.QtWidgets import QApplication

from nostr.article_details import ArticleDetails
from nostr.readiness import NOSTR, STANDUP
from nostr.ui.readiness_view import ReadinessView, check_article
from tests.test_publish_sheets import article

COMPLETE = ArticleDetails(summary="Three days of wind, forty kites and what to bring next year.",
                          image="https://media.example.org/kites.jpg", hashtags=("kites",))


def test_the_sheet_says_nothing_when_all_is_well(tmp_path):
    sheet = article(tmp_path, default_title="Kite festival at the lake", details=COMPLETE)
    assert not sheet._readiness.isVisibleTo(sheet) and sheet._publish_btn.isEnabled()
    sheet.close()


def test_a_blocker_dims_publish_and_show_takes_the_writer_to_its_field(tmp_path):
    sheet = article(tmp_path, default_title="Kite festival at the lake", details=COMPLETE)
    sheet.show()
    sheet._title_edit.setText("")
    result = sheet._readiness.result()
    assert [f.code for f in result.blockers] == ["title.missing"]
    assert sheet._readiness.isVisibleTo(sheet) and not sheet._publish_btn.isEnabled()
    assert sheet._readiness._headline.text() == "1 thing to fix"
    show = sheet._readiness.focus_chain()[1]
    QApplication.processEvents()
    assert not show.visibleRegion().isEmpty()               # the sheet grew: in sight
    show.click()
    QApplication.processEvents()
    assert sheet.focusWidget() is sheet._title_edit
    sheet.close()


def test_suggestions_are_one_folded_line_in_the_sheet(tmp_path):
    sheet = article(tmp_path, default_title="Kite festival at the lake")
    view = sheet._readiness
    assert view.isVisibleTo(sheet) and sheet._publish_btn.isEnabled()
    assert view._headline.text() == "Ready to Publish" and view._more.text() == "· 3 suggestions"
    assert not view._suggestions.isVisibleTo(sheet)
    sheet.show()
    view._more.click()
    QApplication.processEvents()
    assert not view._suggestions.visibleRegion().isEmpty()
    sheet.close()


def test_article_details_shows_the_check_in_full():
    view = ReadinessView(compact=False, is_dark=False)
    result = check_article(body="Text.", title="Kite festival at the lake", link_name="kites",
                           outcome=STANDUP)
    view.show_result(result)
    view.show()
    assert view._headline.text() == "Ready for STANDUP"
    assert view._suggestions.isVisible() and view._suggestions_title.text() == "4 suggestions"
    assert view._checked_toggle.text() == "Checked (5)" and not view._checked.isVisible()
    view._checked_toggle.click()
    assert view._checked.isVisible()
    view.close()


def test_the_cover_is_judged_as_typed_by_the_publishers_rule():
    """The publisher drops a cover it can't publish without a word; the
    check says it, a blocker."""
    inside = check_article(body="Text.", title="T", link_name="t", image="http://192.168.1.10/c.jpg")
    assert [f.code for f in inside.blockers] == ["cover.unsafe"]
    assert check_article(body="Text.", title="T", link_name="t", outcome=NOSTR,
                         image="http://old-blog.example.org/c.jpg").ready
