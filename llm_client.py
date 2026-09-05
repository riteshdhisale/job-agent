"""
LLM client abstraction -- same design as the Agent 1 primer described:
every tool module depends on this interface, never on `anthropic` directly,
so FakeLLMClient can stand in for testing/zero-cost runs.

Extended from the original Agent 1 version to fake five tool schemas now
(discovery's two, plus tailoring, outreach drafting, and interview prep),
since this package runs the whole pipeline, not just discovery.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

try:
    # Load ANTHROPIC_API_KEY / APOLLO_API_KEY / ADZUNA_* etc. from a .env file in the
    # project root, if one exists, so `cp .env.example .env` + fill-in actually works.
    # Real environment variables (if already set) always take priority and are not
    # overwritten. Silently skipped if python-dotenv isn't installed -- everything
    # still works if you set the variables directly in your shell instead.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class LLMClient(Protocol):
    def forced_tool_call(self, system: str, user: str, tool: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def agentic_step(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]],
                      system: Optional[str] = None) -> Any:
        ...


class AnthropicClient:
    """Real Claude API client. Check https://docs.claude.com/en/docs/about-claude/models
    for the current model id if the default below has gone stale; override with CLAUDE_MODEL."""

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        import anthropic
        self.model = model or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "No ANTHROPIC_API_KEY found. Set it in your environment or .env "
                "(see .env.example), or pass --engine fake to any cli.py command "
                "to try it without a real API key."
            )
        self.client = anthropic.Anthropic(api_key=key)

    def forced_tool_call(self, system: str, user: str, tool: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.client.messages.create(
            model=self.model, max_tokens=4096, system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        return {}

    def agentic_step(self, messages, tools, system=None):
        kwargs: Dict[str, Any] = dict(model=self.model, max_tokens=4096, messages=messages, tools=tools)
        if system:
            kwargs["system"] = system
        return self.client.messages.create(**kwargs)


class GeminiClient:
    """Google Gemini API client (the Gemini Developer API via a Google AI Studio key --
    not Vertex AI). A cost-conscious alternative to AnthropicClient: Gemini's API has a
    genuine free tier for personal-scale use. Important: a consumer "Gemini Pro" /
    Gemini Advanced subscription (the one bundled with Google One) does NOT by itself
    grant API access -- you still need a separate API key from
    https://aistudio.google.com/apikey, which is what actually has the free tier.
    Check https://ai.google.dev/gemini-api/docs/models for current model ids if the
    default below has gone stale; override with GEMINI_MODEL. Uses the same forced
    "record_X" tool schemas as AnthropicClient does -- Anthropic's `input_schema` is
    plain JSON Schema, which Gemini's `parameters_json_schema` accepts directly, so
    nothing tool-specific had to be duplicated for this engine.

    Model choice, live-tested (Sept 2026): full Flash models (gemini-2.5-flash,
    gemini-3.6-flash) either 404 for new API keys ("no longer available to new
    users") or carry a very tight free-tier daily request cap (as low as 20/day
    observed live for gemini-3.6-flash -- see README's "Gemini's free tier has a
    real daily request cap" section). gemini-2.5-flash-lite hit the same
    new-user 404. Google's own error for that pointed at gemini-3.5-flash-lite,
    which is also just a better fit for this codebase's actual Gemini usage
    (structured extraction/scoring, not open-ended reasoning) regardless of quota
    -- Flash-Lite models are explicitly positioned for "high-volume
    classification, simple data extraction." If this default 404s for you too,
    check https://ai.google.dev/gemini-api/docs/models for whatever's current and
    override with GEMINI_MODEL rather than assuming this comment is still right."""

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        from google import genai
        from google.genai import types
        import httpx
        self._types = types
        self._httpx = httpx
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "No GEMINI_API_KEY found. Get a free key at https://aistudio.google.com/apikey "
                "(this is separate from any Gemini Pro/Advanced app subscription) and set it "
                "in your environment or .env (see .env.example), or pass --engine fake to any "
                "cli.py command to try it without a real API key."
            )
        # A plain `genai.Client(api_key=key)` (the original version of this line) has no
        # timeout and no retry at all, which bit Ritesh live two runs in a row on a flaky
        # connection: one run got a hard reset mid-request ("[WinError 10053] An
        # established connection was aborted"), the next just hung forever with no error
        # and no way to recover short of killing the whole Flask process. Both failures
        # were the same underlying cause -- a network hiccup between here and Google's
        # API with nothing in place to notice or recover -- so both get fixed together:
        #   - `timeout` bounds a single attempt to 90s (generous, since a 100-job
        #     scoring batch is a large prompt+response) instead of hanging indefinitely.
        #   - `retry_options` retries automatically, with backoff+jitter, on exactly the
        #     transient failures this SDK recognizes: httpx.TimeoutException /
        #     httpx.ConnectError, plus HTTP 408/429/5xx responses (see
        #     google.genai._api_client.retry_args -- this is the SDK's own built-in
        #     mechanism, not something hand-rolled here).
        self.client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=90_000,
                retry_options=types.HttpRetryOptions(attempts=3, initial_delay=2.0, max_delay=20.0),
            ),
        )

    def _build_tool(self, tool_dicts: List[Dict[str, Any]]):
        types = self._types
        return types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters_json_schema=t.get("input_schema", {}),
            )
            for t in tool_dicts
        ])

    # Extra retry attempts for raw connection drops, on top of whatever the client's own
    # http_options.retry_options already retries. Two distinct live incidents drove this,
    # and both matter for exactly which exception types are caught below:
    #   - "[WinError 10053] An established connection was aborted" surfaced as a bare
    #     OSError/ConnectionError -- not one of the httpx exception types the SDK's
    #     built-in retry recognizes as transient (see google.genai._api_client:
    #     _HTTPX_TRANSIENT_EXC = httpx.TimeoutException/httpx.ConnectError only), so it
    #     wouldn't get retried at all without this.
    #   - "[WinError 10054] An existing connection was forcibly closed by the remote
    #     host" (a reset mid-response, while reading Gemini's reply) surfaced as
    #     httpx.ReadError instead -- confirmed NOT an OSError/ConnectionError subclass
    #     (httpx.ReadError's MRO is its own hierarchy: NetworkError -> TransportError ->
    #     RequestError -> HTTPError), so the original `except (OSError, ConnectionError)`
    #     from the first fix silently let this one crash the whole discover run instead
    #     of retrying it -- confirmed live. `httpx.TransportError` is the broad parent of
    #     ReadError/ConnectError/WriteError/CloseError/*Timeout/ProxyError/etc., so
    #     catching that (instead of trying to enumerate every specific httpx exception
    #     that might show up next) is the actual fix, not a narrower patch for just
    #     ReadError. Deliberately still excludes real API errors (bad key, unknown model,
    #     genuine quota exhaustion) -- those are never httpx.TransportError/OSError/
    #     ConnectionError, so they still surface immediately instead of retrying
    #     something that will never succeed.
    _CONNECTION_RETRY_ATTEMPTS = 3
    _CONNECTION_RETRY_BACKOFF_SECONDS = 3

    def forced_tool_call(self, system: str, user: str, tool: Dict[str, Any]) -> Dict[str, Any]:
        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[self._build_tool([tool])],
            # mode="ANY" forces a function call rather than a plain text reply --
            # the Gemini equivalent of Anthropic's tool_choice={"type": "tool", ...}.
            # Only one function is declared above, so this can only call that one.
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
        )

        resp = None
        for attempt in range(1, self._CONNECTION_RETRY_ATTEMPTS + 1):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=user, config=config)
                break
            except (OSError, ConnectionError, self._httpx.TransportError):
                if attempt == self._CONNECTION_RETRY_ATTEMPTS:
                    raise
                time.sleep(self._CONNECTION_RETRY_BACKOFF_SECONDS * attempt)

        calls = getattr(resp, "function_calls", None)
        if calls:
            return dict(calls[0].args)
        return {}

    def agentic_step(self, messages, tools, system=None):
        # Not exercised by job_agent today -- every tool module here uses
        # forced_tool_call's single-shot structured output, never a multi-turn
        # tool-choice loop. Kept for interface parity with AnthropicClient, and
        # only translates the most recent user message (a real loop would need
        # fuller history translation between Anthropic's and Gemini's message shapes).
        types = self._types
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        config = types.GenerateContentConfig(tools=[self._build_tool(tools)])
        if system:
            config.system_instruction = system
        return self.client.models.generate_content(model=self.model, contents=last_user, config=config)


_ROLE_KEYWORDS = [
    "Manager", "Engineer", "Analyst", "Developer", "Specialist", "Lead", "Director",
    "Intern", "Designer", "Scientist", "Architect", "Owner", "Consultant", "Associate",
    "Representative", "Executive",
]
_ROLE_PATTERN = re.compile(r"([A-Z][A-Za-z0-9,&/\- ]{3,60}(?:%s))" % "|".join(_ROLE_KEYWORDS))
_WORD_PATTERN = re.compile(r"[a-z]{4,}")


class FakeLLMClient:
    """No API key, no network cost, no real intelligence -- crude rule-based
    stand-ins for every tool in this package, purely to prove wiring is
    correct (see tests/). Do NOT judge output quality from this. Swap in
    AnthropicClient for anything real."""

    def forced_tool_call(self, system: str, user: str, tool: Dict[str, Any]) -> Dict[str, Any]:
        handler = {
            "record_job_listings": self._fake_extract,
            "record_match_scores": self._fake_score,
            "record_tailored_resume": self._fake_tailor,
            "record_outreach_draft": self._fake_outreach,
            "record_interview_prep": self._fake_interview_prep,
            "record_linkedin_post": self._fake_linkedin_post,
            "record_application_email": self._fake_application_email,
        }.get(tool["name"])
        if handler is None:
            raise ValueError(f"FakeLLMClient doesn't know how to fake tool '{tool['name']}'")
        return handler(user)

    def agentic_step(self, messages, tools, system=None):
        raise NotImplementedError("Autonomous tool-choice loops need a real ANTHROPIC_API_KEY.")

    # --- discovery (unchanged from Agent 1) ---

    def _fake_extract(self, user_prompt: str) -> Dict[str, Any]:
        candidates = _ROLE_PATTERN.findall(user_prompt)
        seen, listings = set(), []
        for raw_title in candidates:
            title = raw_title.strip(" ,")
            if title in seen or len(listings) >= 8:
                continue
            seen.add(title)
            listings.append({
                "title": title, "location": "(fake extractor doesn't parse location)",
                "url": "", "snippet": f"(fake extractor) matched role keyword in: '{title}'",
            })
        return {"listings": listings}

    def _fake_score(self, user_prompt: str) -> Dict[str, Any]:
        resume_match = re.search(r"<candidate_resume>(.*?)</candidate_resume>", user_prompt, re.S)
        listings_match = re.search(r"<job_listings>(.*?)</job_listings>", user_prompt, re.S)
        resume_words = set(_WORD_PATTERN.findall((resume_match.group(1) if resume_match else "").lower()))
        scores = []
        if listings_match:
            entries = re.split(r"\[\d+\]", listings_match.group(1))[1:]
            for entry in entries:
                entry_words = set(_WORD_PATTERN.findall(entry.lower()))
                overlap = resume_words & entry_words
                score = min(100, int(100 * len(overlap) / max(8, len(entry_words))))
                title_match = re.search(r"Title:\s*(.+)", entry)
                scores.append({
                    "title": title_match.group(1).strip() if title_match else "",
                    "score": score,
                    "rationale": f"(fake scorer) {len(overlap)} overlapping keyword(s): "
                                 f"{', '.join(sorted(overlap)[:5]) or 'none'}",
                    "matched_skills": sorted(overlap)[:5], "missing_skills": [],
                })
        return {"scores": scores}

    # --- tailoring, outreach, interview prep: crude but structurally valid fakes ---

    def _fake_tailor(self, user_prompt: str) -> Dict[str, Any]:
        job_match = re.search(r"Title:\s*(.+)", user_prompt)
        job_title = job_match.group(1).strip() if job_match else "this role"
        return {
            "tailored_summary": f"(fake tailor) Summary re-emphasized toward: {job_title}.",
            "tailored_bullets": [
                {"original": "(fake) original bullet", "tailored": f"(fake) bullet re-worded for {job_title}",
                 "reason": "(fake tailor) keyword alignment only, not real judgment"},
            ],
            "flagged_gaps": [],
        }

    def _fake_outreach(self, user_prompt: str) -> Dict[str, Any]:
        name_match = re.search(r"Name:\s*(.+)", user_prompt)
        name = name_match.group(1).strip() if name_match else "there"
        return {
            "subject_line": "(fake outreach) Quick note about an open role",
            "message": f"(fake outreach draft) Hi {name}, this is a placeholder message -- "
                       f"use --engine claude for a real draft.",
        }

    def _fake_interview_prep(self, user_prompt: str) -> Dict[str, Any]:
        return {
            "likely_questions": [
                {"question": "(fake) Walk me through a project relevant to this role.",
                 "why_theyll_ask": "(fake) generic opener", "how_to_answer": "(fake) use your strongest matched bullet"},
            ],
            "questions_to_ask_them": ["(fake) What does success look like in this role after 90 days?"],
            "watch_out_for": [],
        }

    def _fake_linkedin_post(self, user_prompt: str) -> Dict[str, Any]:
        text = user_prompt
        looks_like_a_job = bool(_ROLE_PATTERN.search(text)) or "hiring" in text.lower()
        if not looks_like_a_job:
            return {"is_job_posting": False, "title": "", "company": "", "location": "",
                     "snippet": "", "application_method": "unclear", "application_target": "",
                     "poster_name": "", "poster_title": ""}

        title_match = _ROLE_PATTERN.search(text)
        email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        url_match = re.search(r"https?://\S+", text)
        if email_match:
            method, target = "email", email_match.group(0)
        elif url_match:
            method, target = "link", url_match.group(0)
        elif "dm" in text.lower() or "message me" in text.lower():
            method, target = "dm", "(fake extractor) DM the poster"
        else:
            method, target = "unclear", ""

        return {
            "is_job_posting": True,
            "title": title_match.group(0).strip() if title_match else "(fake) role unclear",
            "company": "(fake extractor doesn't parse company)",
            "location": "(fake extractor doesn't parse location)",
            "snippet": "(fake extractor) parsed from a pasted LinkedIn post",
            "application_method": method,
            "application_target": target,
            "poster_name": "", "poster_title": "",
        }

    def _fake_application_email(self, user_prompt: str) -> Dict[str, Any]:
        return {
            "subject": "(fake) Application for the role",
            "body": "(fake application email draft) placeholder -- use --engine claude for a real draft.",
        }
