"""Tests for score_job_matches -- specifically the batching/chunking added
after Adzuna started returning up to 250 listings in one run (a single
250-listing prompt risks the model truncating its structured output
partway through, which would otherwise silently read as "the rest scored
zero" rather than surfacing as a problem). No real LLM needed -- a fake
client records how many calls it received and how large each one was."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.extract_tool import JobListing
from tools.score_tool import score_job_matches, SCORE_BATCH_SIZE


class _RecordingClient:
    """Fakes forced_tool_call: returns one score per listing found in the
    prompt (by counting "[" markers), and records each call's batch size."""

    def __init__(self):
        self.calls = []

    def forced_tool_call(self, system, user, tool):
        n = user.count("] Title:")
        self.calls.append(n)
        return {"scores": [
            {"title": f"job{i}", "score": 50, "rationale": "ok",
             "matched_skills": [], "missing_skills": []}
            for i in range(n)
        ]}


def _jobs(n):
    return [JobListing(title=f"Job {i}", location="Remote", url=f"http://x/{i}",
                        snippet="desc", source_url="http://x") for i in range(n)]


def test_small_list_stays_in_one_call():
    client = _RecordingClient()
    matches = score_job_matches(client, "resume", _jobs(5))
    assert len(matches) == 5
    assert client.calls == [5]


def test_large_list_splits_into_batches_of_score_batch_size():
    """250 listings (Adzuna's realistic max per run) should split into
    ceil(250 / SCORE_BATCH_SIZE) calls, none larger than SCORE_BATCH_SIZE."""
    n = 250
    client = _RecordingClient()
    matches = score_job_matches(client, "resume", _jobs(n))
    assert len(matches) == n
    assert all(c <= SCORE_BATCH_SIZE for c in client.calls)
    assert sum(client.calls) == n
    assert len(client.calls) == -(-n // SCORE_BATCH_SIZE)  # ceil division


def test_exact_multiple_of_batch_size_does_not_add_an_empty_trailing_batch():
    n = SCORE_BATCH_SIZE * 3
    client = _RecordingClient()
    score_job_matches(client, "resume", _jobs(n))
    assert client.calls == [SCORE_BATCH_SIZE, SCORE_BATCH_SIZE, SCORE_BATCH_SIZE]


def test_empty_list_makes_no_calls():
    client = _RecordingClient()
    assert score_job_matches(client, "resume", []) == []
    assert client.calls == []


def test_a_short_final_batch_scores_correctly():
    n = SCORE_BATCH_SIZE + 4
    client = _RecordingClient()
    matches = score_job_matches(client, "resume", _jobs(n))
    assert len(matches) == n
    assert client.calls == [SCORE_BATCH_SIZE, 4]


class _TruncatingClient:
    """Always returns 3 fewer scores than listings sent -- simulates the
    model cutting its structured output short partway through a batch."""

    def forced_tool_call(self, system, user, tool):
        n = user.count("] Title:")
        short = max(n - 3, 0)
        return {"scores": [
            {"title": f"job{i}", "score": 50, "rationale": "ok",
             "matched_skills": [], "missing_skills": []}
            for i in range(short)
        ]}


def test_truncated_batch_warns_and_returns_only_the_scores_it_got(capsys):
    matches = score_job_matches(_TruncatingClient(), "resume", _jobs(10))
    assert len(matches) == 7  # 10 sent, 3 "lost" to simulated truncation
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "10" in captured.err and "7" in captured.err
