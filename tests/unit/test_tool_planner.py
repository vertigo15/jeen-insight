"""Phase 3 tests: post-result tool planner intent detection.

Intent MUST come only from the user's question (never data), and a normal
analytics question must NOT propose an outbound action.
"""

from __future__ import annotations

from src.agent.tool_planner import (
    default_subject,
    detect_jira_intent,
    detect_send_email_intent,
    detect_slack_post_intent,
    detect_web_search_intent,
)


class TestIntentDetection:
    def test_plain_analytics_question_no_intent(self):
        assert detect_send_email_intent("show sales per month for 2006 and 2007") is None

    def test_question_mentioning_mail_column_no_action_verb(self):
        # "email" appears as a data noun but there is no send/share verb.
        assert detect_send_email_intent("how many customers have a gmail email address?") is None

    def test_explicit_email_with_recipient(self):
        intent = detect_send_email_intent("email these results to alice@corp.com")
        assert intent is not None
        assert intent["recipients"] == ["alice@corp.com"]

    def test_explicit_email_multiple_recipients_deduped(self):
        intent = detect_send_email_intent(
            "send this by email to a@x.com and A@X.com and bob@y.com"
        )
        assert intent is not None
        assert intent["recipients"] == ["a@x.com", "bob@y.com"]

    def test_email_to_me_resolves_self_address(self):
        intent = detect_send_email_intent(
            "email this to me please", self_address="owner@corp.com"
        )
        assert intent is not None
        assert intent["recipients"] == ["owner@corp.com"]

    def test_email_intent_without_recipient_returns_empty_list(self):
        # Intent is clear (email + send) but no recipient -> empty list so the
        # confirm card forces the user to enter one.
        intent = detect_send_email_intent("please email this report")
        assert intent is not None
        assert intent["recipients"] == []

    def test_empty_question(self):
        assert detect_send_email_intent("") is None
        assert detect_send_email_intent("   ") is None


class TestWebSearchIntent:
    def test_plain_analytics_question_no_intent(self):
        # A normal analytics question must NEVER trigger an external web call.
        assert detect_web_search_intent("show sales per month for 2006 and 2007") is None

    def test_question_mentioning_web_column_no_signal(self):
        # "website" is a data noun; no explicit search-the-web signal.
        assert detect_web_search_intent("how many customers have a website?") is None

    def test_explicit_search_the_web(self):
        intent = detect_web_search_intent("search the web for the latest EV market share")
        assert intent is not None
        assert intent["query"] == "search the web for the latest EV market share"

    def test_explicit_internet_signal(self):
        assert detect_web_search_intent("what's the latest news on interest rates?") is not None
        assert detect_web_search_intent("find the CEO of Acme on the internet") is not None

    def test_query_is_capped(self):
        intent = detect_web_search_intent("search the web for " + "x" * 1000)
        assert intent is not None
        assert len(intent["query"]) <= 400

    def test_empty_question(self):
        assert detect_web_search_intent("") is None
        assert detect_web_search_intent("   ") is None


class TestSlackIntent:
    def test_plain_analytics_no_intent(self):
        assert detect_slack_post_intent("show sales per month for 2006") is None

    def test_mentions_slack_without_verb_no_intent(self):
        # "slack" as a noun with no post/share verb must not fire.
        assert detect_slack_post_intent("how many users signed up via slack?") is None

    def test_explicit_post_to_slack(self):
        intent = detect_slack_post_intent("post this to slack")
        assert intent is not None
        assert intent["channel"] == ""

    def test_channel_token_extracted(self):
        intent = detect_slack_post_intent("share these results in slack #sales-eu")
        assert intent is not None
        assert intent["channel"] == "#sales-eu"

    def test_empty_question(self):
        assert detect_slack_post_intent("") is None
        assert detect_slack_post_intent("   ") is None


class TestJiraIntent:
    def test_plain_analytics_no_intent(self):
        assert detect_jira_intent("show sales per month for 2006") is None

    def test_mentions_jira_without_verb_no_intent(self):
        assert detect_jira_intent("how many tickets came from jira last week?") is None

    def test_explicit_create_jira_issue(self):
        assert detect_jira_intent("create a jira issue for this") is not None
        assert detect_jira_intent("open a jira ticket to track this") is not None
        assert detect_jira_intent("file a bug in jira") is not None

    def test_empty_question(self):
        assert detect_jira_intent("") is None
        assert detect_jira_intent("   ") is None


class TestDefaultSubject:
    def test_subject_prefixed_and_capped(self):
        s = default_subject("top 5 products by revenue")
        assert s.startswith("Jeen Insights: ")
        assert "top 5 products by revenue" in s

    def test_subject_length_capped(self):
        s = default_subject("x" * 500)
        assert len(s) <= 200

    def test_empty_question_has_fallback(self):
        assert default_subject("") == "Jeen Insights result"
