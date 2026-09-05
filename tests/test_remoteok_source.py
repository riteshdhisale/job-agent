"""Tests for the RemoteOK source -- specifically multi-tag matching, added so
one run can search "product manager", "product owner", and "business
analyst" simultaneously without fetching the (free, public) feed three
times. Mocked HTTP throughout, no network needed."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.remoteok_source import fetch_remoteok_listings

FEED = [
    {"legal": "notice"},  # RemoteOK's real feed always starts with a non-job object
    {"position": "Senior Product Manager", "tags": ["product", "saas"], "location": "Remote",
     "url": "http://x/1", "description": "Own the roadmap."},
    {"position": "Business Analyst", "tags": ["analytics"], "location": "Remote",
     "url": "http://x/2", "description": "Analyze things."},
    {"position": "Backend Engineer", "tags": ["python", "backend"], "location": "Remote",
     "url": "http://x/3", "description": "Ship things."},
]


def _mock_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = FEED
    return resp


@patch("tools.remoteok_source.requests.get", return_value=_mock_response())
def test_single_tag_matches_only_that_tag(mock_get):
    result = fetch_remoteok_listings(tag="product")
    assert result.ok
    assert [j.title for j in result.listings] == ["Senior Product Manager"]


@patch("tools.remoteok_source.requests.get", return_value=_mock_response())
def test_multiple_tags_match_any_of_them(mock_get):
    result = fetch_remoteok_listings(tag=["product", "business analyst"])
    titles = {j.title for j in result.listings}
    assert titles == {"Senior Product Manager", "Business Analyst"}
    assert "Backend Engineer" not in titles


@patch("tools.remoteok_source.requests.get", return_value=_mock_response())
def test_multiple_tags_still_fetch_the_feed_only_once(mock_get):
    fetch_remoteok_listings(tag=["product", "business analyst", "product owner"])
    assert mock_get.call_count == 1


@patch("tools.remoteok_source.requests.get", return_value=_mock_response())
def test_a_listing_matching_two_tags_is_not_duplicated(mock_get):
    # "Senior Product Manager" matches both "product" (tag) and "manager" (title
    # substring) if both were passed -- should still appear exactly once.
    result = fetch_remoteok_listings(tag=["product", "manager"])
    titles = [j.title for j in result.listings]
    assert titles.count("Senior Product Manager") == 1
