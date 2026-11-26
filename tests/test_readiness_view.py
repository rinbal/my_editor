"""The Publication Check on screen: the Publish sheet, Article Details and
the page's line (nostr/ui/readiness_view.py)."""
from PySide6.QtCore import Qt
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
    assert sheet._title_problem.text() == "Add a title before publishing."   # in place too
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
    assert view._headline.text() == "Ready to Publish" and view._more.text() == "3 suggestions"
    assert not view._suggestions.isVisibleTo(sheet)
    sheet.show()
    view._more.click()
    QApplication.processEvents()
    assert not view._suggestions.visibleRegion().isEmpty()
    sheet.close()


def test_article_details_shows_the_check_in_full_and_tab_follows_it():
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget
    page = QWidget()
    column = QVBoxLayout(page)
    view = ReadinessView(compact=False, is_dark=False)
    after = QLineEdit()                                     # Article Details' Title, under it
    column.addWidget(view)
    column.addWidget(after)
    view.show_result(check_article(body="Text.", title="Kite festival at the lake",
                                   link_name="kites", outcome=STANDUP, summary="Short."))
    result = check_article(body="Text.", title="Kite festival at the lake", link_name="kites",
                           outcome=STANDUP)
    view.show_result(result)                                # its rows made anew
    page.show()

    def tab_from(widget):
        """Where Tab goes from ``widget``: the next control that takes it."""
        step = widget.nextInFocusChain()
        while not (step.focusPolicy() & Qt.FocusPolicy.TabFocus and step.isVisibleTo(page)
                   and step.isEnabled()):
            step = step.nextInFocusChain()
        return step
    shows = view.focus_chain()[1:-1]
    assert len(shows) == 4                                  # one Show per suggestion
    assert [tab_from(w) for w in shows] == [*shows[1:], view._checked_toggle]
    assert tab_from(view._checked_toggle) is after          # then Article Details' fields
    assert view._headline.text() == "Ready for STANDUP"
    assert view._suggestions.isVisible() and view._suggestions_title.text() == "4 suggestions"
    assert view._checked_toggle.text() == "Checked (5)" and not view._checked.isVisible()
    view._checked_toggle.click()
    assert view._checked.isVisible()
    view.close()


def test_what_only_standup_refuses_never_keeps_an_article_from_the_relays():
    """Publish as Article sends to the writer's relays: STANDUP's size is
    said, as a suggestion, and the article is Ready to Publish."""
    result = check_article(body="ä" * 50_000, title="Kite festival at the lake",
                           link_name="kites", outcome=STANDUP)
    assert result.ready and result.outcome == NOSTR
    assert "size.standup" in [f.code for f in result.suggestions]


def test_a_link_name_of_spaces_is_an_empty_one():
    assert [f.code for f in check_article(body="Text.", title="T", link_name="  ").blockers] \
        == ["slug.missing"]


def test_the_cover_is_judged_as_typed_by_the_publishers_rule():
    """The publisher drops a cover it can't publish without a word; the
    check says it, a blocker."""
    inside = check_article(body="Text.", title="T", link_name="t", image="http://192.168.1.10/c.jpg")
    assert [f.code for f in inside.blockers] == ["cover.local"]
    assert check_article(body="Text.", title="T", link_name="t", outcome=NOSTR,
                         image="http://old-blog.example.org/c.jpg").ready
