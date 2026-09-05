"""
A basic local web UI over the exact same engine cli.py uses -- same
profile_store, job_store, discovery, and tools/* modules, just a different
front end. Nothing here duplicates business logic; every route is a thin
wrapper that calls the same functions cli.py calls.

Run it:
    python app.py
Then open http://127.0.0.1:5000 in your browser. See README.md for details.

This is a single-user, local-only tool: no login, no auth, binds to
127.0.0.1 by default. Do not expose this to the open internet as-is.

--- UI architecture (rewritten 2026-09-04) ---------------------------------
The whole "browse jobs -> view one -> tailor/contact/apply/prep it" loop now
lives on ONE page (templates/index.html): a job list on the left, a detail
panel on the right that updates in place, and an inline Discover panel with
its own live progress bar -- none of that used to leave the page or hit a
full page reload before. This came directly from live feedback ("too many
tabs/pages, feels disjointed") after Ritesh actually used the multi-page
version end to end.

Every route that used to render a full HTML page and redirect back with a
flash message (tailor, contacts, outreach, apply, apply/resolve,
mark-applied, delete, prep, discover) now returns JSON instead, called via
fetch() from index.html's JS. Two small, self-contained flows stay as
their own traditional pages -- Add Lead / Add Browsed Page / Profile --
since they're occasional actions, not part of the main loop, and a plain
form-post-and-redirect is the simplest honest fit for them.
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

from llm_client import AnthropicClient, FakeLLMClient, GeminiClient
from profile_store import ProfileStore
from job_store import JobStore, STATUSES
from discovery import run_discovery
from tools.tailor_tool import tailor_resume, MatchScoreTooLowError, MIN_MATCH_SCORE_TO_TAILOR
from tools.contact_tool import find_contacts
from tools.outreach_tool import draft_outreach_message
from tools.apply_tool import fill_application_form, draft_application_email
from tools.interview_tool import prepare_for_interview
from tools.manual_lead_tool import parse_pasted_lead
from tools.extract_tool import extract_job_listings
from tools.score_tool import score_job_matches
from tools.instahyre_scraper import fetch_instahyre_listings

DATA_DIR = Path(__file__).resolve().parent / "data"

app = Flask(__name__)
# Local single-user tool -- a fixed dev key is fine since this never leaves your machine.
app.secret_key = "job-agent-local-dev-key"


def _format_lakhs(value: Optional[int]) -> Optional[str]:
    """30 LPA is how salary actually gets talked about here, not "3,000,000" --
    this just renders a raw annual-salary integer that way. None stays None so
    callers can show "not disclosed" instead of a filtered garbage value."""
    if value is None:
        return None
    lakhs = value / 100000
    text = f"{lakhs:.1f}".rstrip("0").rstrip(".")
    return f"₹{text}L"


def _format_salary(job) -> str:
    if job.salary_min and job.salary_max and job.salary_min != job.salary_max:
        return f"{_format_lakhs(job.salary_min)}–{_format_lakhs(job.salary_max)}"
    if job.salary_max:
        return _format_lakhs(job.salary_max)
    if job.salary_min:
        return _format_lakhs(job.salary_min)
    return "not disclosed"


def _time_ago(posted_at) -> Optional[str]:
    """Renders an ISO-ish posted_at timestamp (currently only Adzuna provides one --
    see tools/adzuna_source.py) as "posted 6h ago" instead of a raw timestamp. Returns
    None (not an error string) for anything missing/unparseable, so callers can fall
    back to "posting date unknown" the same way they already do for missing salary."""
    if not posted_at:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if hours < 0:
        return None
    if hours < 1:
        return "posted <1h ago"
    if hours < 48:
        return f"posted {int(hours)}h ago"
    return f"posted {int(hours // 24)}d ago"


def _job_to_dict(job) -> dict:
    """JSON shape for one job, used by every /api/jobs* route -- computed display
    strings (salary, posted-age, best link to apply at) live here once instead of
    being recomputed in every JS render call."""
    d = asdict(job)
    d["salary_display"] = _format_salary(job)
    d["posted_display"] = _time_ago(job.posted_at) or "posting date unknown"
    d["target_url"] = job.application_target or job.url or job.source_url
    return d


_ENGINES = {"fake": FakeLLMClient, "claude": AnthropicClient, "gemini": GeminiClient}


def get_client(engine: str):
    cls = _ENGINES.get(engine, FakeLLMClient)
    return cls()


def current_engine() -> str:
    return session.get("engine", "fake")


# --- background discovery run + progress bar -------------------------------
# discover can take anywhere from seconds to a couple of minutes (one real LLM
# call per career site, plus each API source), which used to leave the browser
# just sitting on a spinner with zero feedback. This runs it on a background
# thread instead and exposes its progress over /discover/status for the
# dashboard's inline progress panel to poll. Single-user local tool -- one
# discovery run at a time is a deliberate simplification, not a bug.
_discover_lock = threading.Lock()
_discover_state = {"running": False, "done": 0, "total": 0, "label": "", "jobs_found": 0,
                    "error": None, "finished": False, "summary": None}


def _discover_worker(engine: str, query: str, country: str, include_instahyre: bool,
                      max_age_hours: Optional[int] = None, min_salary: Optional[int] = None,
                      title_keywords: Optional[list] = None, exclude_title_keywords: Optional[list] = None,
                      allowed_locations: Optional[list] = None) -> None:
    global _discover_state
    try:
        client = get_client(engine)
        resume_text = Path(DATA_DIR / "resume.txt").read_text()
        site_urls = json.loads(Path(DATA_DIR / "career_sites.json").read_text())
        gh_path, lv_path = DATA_DIR / "greenhouse_boards.json", DATA_DIR / "lever_sites.json"
        greenhouse_boards = json.loads(gh_path.read_text()) if gh_path.exists() else []
        lever_sites = json.loads(lv_path.read_text()) if lv_path.exists() else []
        store = JobStore()

        def on_progress(evt):
            with _discover_lock:
                _discover_state.update(done=evt["done"], total=evt["total"], label=evt["label"],
                                        jobs_found=evt["jobs_found"])

        report = run_discovery(client, resume_text, site_urls, store, query=query, country=country,
                                greenhouse_boards=greenhouse_boards, lever_sites=lever_sites,
                                max_age_hours=max_age_hours, min_salary=min_salary,
                                title_keywords=title_keywords or (), exclude_title_keywords=exclude_title_keywords or (),
                                allowed_locations=allowed_locations or (),
                                on_progress=on_progress)

        jobs_found = report.jobs_found
        if include_instahyre:
            with _discover_lock:
                _discover_state.update(label="Instahyre (rate-limited)")
            # Instahyre takes one search term -- use just the first of possibly several
            # comma-separated roles from the query field.
            instahyre = fetch_instahyre_listings(query=query.split(",")[0].strip())
            if not instahyre.ok:
                report.sites_failed.append(f"instahyre -- {instahyre.note}")
            elif instahyre.listings:
                for match in score_job_matches(client, resume_text, instahyre.listings):
                    store.add_match(match, source_type="scraped")
                    jobs_found += 1

        with _discover_lock:
            _discover_state.update(
                running=False, finished=True,
                summary={"jobs_found": jobs_found, "sites_attempted": report.sites_attempted,
                          "sites_failed": report.sites_failed},
            )
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the progress panel instead of hanging it
        traceback.print_exc()
        with _discover_lock:
            _discover_state.update(running=False, finished=True, error=str(exc))


@app.context_processor
def inject_globals():
    return {"current_engine": current_engine(), "STATUSES": STATUSES}


@app.route("/settings/engine", methods=["POST"])
def set_engine():
    session["engine"] = request.form.get("engine", "fake")
    return redirect(request.referrer or url_for("index"))


@app.route("/")
def index():
    # The dashboard itself is one template; everything in it (job list, job detail,
    # discover panel) is populated by JS calling the /api/* routes below. `open_job`
    # is an optional query param (e.g. after Add Lead adds a job) telling the page
    # which job to auto-select on load.
    open_job = request.args.get("open")
    return render_template("index.html", open_job=open_job, min_score=MIN_MATCH_SCORE_TO_TAILOR)


# --- JSON API for the dashboard ---------------------------------------------

@app.route("/api/jobs")
def api_jobs():
    store = JobStore()
    status = request.args.get("status") or None
    jobs = store.list(status=status)
    all_jobs = store.list() if status else jobs
    counts = {s: 0 for s in STATUSES}
    for j in all_jobs:
        counts[j.status] = counts.get(j.status, 0) + 1
    applied_or_further = sum(counts[s] for s in ("applied", "interviewing", "closed"))
    return jsonify({
        "jobs": [_job_to_dict(j) for j in jobs],
        "counts": counts,
        "total": len(all_jobs),
        "applied_or_further": applied_or_further,
    })


@app.route("/api/jobs/<int:job_id>")
def api_job_detail(job_id):
    job = JobStore().get(job_id)
    if not job:
        return jsonify({"ok": False, "error": f"No job #{job_id}"}), 404
    return jsonify({"ok": True, "job": _job_to_dict(job)})


@app.route("/api/jobs/<int:job_id>/status", methods=["POST"])
def api_job_status(job_id):
    new_status = (request.json or {}).get("status", "")
    try:
        JobStore().set_status(job_id, new_status)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/jobs/<int:job_id>/tailor", methods=["POST"])
def api_tailor(job_id):
    store = JobStore()
    job = store.get(job_id)
    resume_text = Path(DATA_DIR / "resume.txt").read_text()
    force = bool((request.json or {}).get("force"))
    try:
        result = tailor_resume(get_client(current_engine()), resume_text, job.title, job.snippet,
                                job.score, force=force)
    except MatchScoreTooLowError as e:
        return jsonify({"ok": False, "error": str(e)})

    bullets_text = "\n".join(f"- {b.tailored}  [{b.reason}]" for b in result.tailored_bullets)
    tailored_text = f"{result.tailored_summary}\n\n{bullets_text}"
    if result.flagged_gaps:
        tailored_text += "\n\nFlagged gaps:\n" + "\n".join(f"- {g}" for g in result.flagged_gaps)
    store.update(job_id, tailored_resume=tailored_text)
    store.set_status(job_id, "tailored")
    return jsonify({"ok": True, "tailored_resume": tailored_text, "status": "tailored"})


@app.route("/api/jobs/<int:job_id>/contacts", methods=["POST"])
def api_contacts(job_id):
    store = JobStore()
    job = store.get(job_id)
    if job.outreach_contacts:
        return jsonify({"ok": True, "contacts": job.outreach_contacts, "info": "Already have contact(s) for this job."})

    company = (request.json or {}).get("company", "").strip()
    if not company:
        return jsonify({"ok": False, "error": "Company name is required to look up contacts."})

    found = find_contacts(company, department_hint="product")
    if not found:
        return jsonify({"ok": True, "contacts": [],
                         "info": "No contacts found (no APOLLO_API_KEY set, or nothing matched). "
                                 "You can still draft a message manually below."})

    contacts_json = [{"name": c.name, "title": c.title, "linkedin_url": c.linkedin_url,
                       "email": c.email, "source": c.source} for c in found]
    store.update(job_id, outreach_contacts=json.dumps(contacts_json))
    return jsonify({"ok": True, "contacts": contacts_json})


@app.route("/api/jobs/<int:job_id>/outreach", methods=["POST"])
def api_outreach(job_id):
    store = JobStore()
    job = store.get(job_id)
    resume_text = Path(DATA_DIR / "resume.txt").read_text()
    body = request.json or {}

    name = body.get("name", "").strip()
    title = body.get("title", "").strip()
    company = body.get("company", "").strip() or "the company"

    if name:
        recipient_name, recipient_title = name, title
    elif job.outreach_contacts:
        top = job.outreach_contacts[0]
        recipient_name, recipient_title = top.get("name", ""), top.get("title", "")
    else:
        return jsonify({"ok": False, "error": "No contact on file -- find contacts first, or fill in a name manually."})

    draft = draft_outreach_message(get_client(current_engine()), resume_text, job.title, company,
                                    job.rationale, recipient_name, recipient_title)
    message_text = f"To: {draft.recipient_name} ({draft.recipient_title})\nSubject: {draft.subject_line}\n\n{draft.message}"
    store.update(job_id, outreach_message=message_text)
    store.set_status(job_id, "contacted")
    return jsonify({"ok": True, "outreach_message": message_text, "status": "contacted"})


@app.route("/api/jobs/<int:job_id>/apply", methods=["POST"])
def api_apply(job_id):
    store = JobStore()
    profile_store = ProfileStore()
    profile = profile_store.load()
    job = store.get(job_id)
    client = get_client(current_engine())
    company = (request.json or {}).get("company", "").strip() or "the company"

    if job.application_method == "email":
        draft = draft_application_email(client, job.title, company, job.rationale, job.application_target or "")
        notes = f"To: {draft.to}\nSubject: {draft.subject}\n\n{draft.body}"
        store.update(job_id, application_notes=notes)
        store.set_status(job_id, "ready_to_submit")
        return jsonify({"ok": True, "kind": "email_drafted", "notes": notes, "status": "ready_to_submit"})

    if job.application_method in ("dm", "unclear"):
        return jsonify({"ok": True, "kind": "manual_required",
                         "info": f"Application method is '{job.application_method}' -- target: "
                                 f"{job.application_target or '(not specified)'}. No safe automation for this; handle it yourself."})

    url = job.application_target or job.url
    screenshot_path = str(DATA_DIR / f"apply_screenshot_job{job_id}.png")
    result = fill_application_form(url, profile, screenshot_path=screenshot_path)

    if result.unmatched_fields:
        session["pending_apply"] = {"job_id": job_id, "labels": result.unmatched_fields,
                                     "filled": [f.label for f in result.filled],
                                     "screenshot": result.screenshot_path}
        return jsonify({"ok": True, "kind": "needs_review",
                         "pending": {"labels": result.unmatched_fields,
                                     "filled": [f.label for f in result.filled],
                                     "screenshot": result.screenshot_path}})

    notes = f"Filled: {[f.label for f in result.filled]}\nScreenshot: {result.screenshot_path}"
    store.update(job_id, application_notes=notes)
    store.set_status(job_id, "ready_to_submit")
    return jsonify({"ok": True, "kind": "filled", "notes": notes, "status": "ready_to_submit"})


@app.route("/api/jobs/<int:job_id>/apply/resolve", methods=["POST"])
def api_apply_resolve(job_id):
    pending = session.pop("pending_apply", None)
    if not pending or pending.get("job_id") != job_id:
        return jsonify({"ok": False, "error": "No pending application review for that job."}), 400

    answers = (request.json or {}).get("answers", [])
    profile_store = ProfileStore()
    profile = profile_store.load()
    for label, answer in zip(pending["labels"], answers):
        answer = (answer or "").strip()
        if answer:
            profile.remember(label, answer)
    profile_store.save(profile)

    store = JobStore()
    notes = (f"Filled: {pending['filled']}\nAnswered and saved: {pending['labels']}\n"
             f"Screenshot: {pending['screenshot']}")
    store.update(job_id, application_notes=notes)
    store.set_status(job_id, "ready_to_submit")
    return jsonify({"ok": True, "notes": notes, "status": "ready_to_submit"})


@app.route("/api/jobs/<int:job_id>/mark-applied", methods=["POST"])
def api_mark_applied(job_id):
    JobStore().set_status(job_id, "applied")
    return jsonify({"ok": True, "status": "applied"})


@app.route("/api/jobs/<int:job_id>/delete", methods=["POST"])
def api_delete_job(job_id):
    if JobStore().delete(job_id):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": f"No job #{job_id} found."}), 404


@app.route("/api/jobs/<int:job_id>/prep", methods=["POST"])
def api_prep(job_id):
    store = JobStore()
    job = store.get(job_id)
    prep_result = prepare_for_interview(get_client(current_engine()), job.tailored_resume or job.rationale,
                                         job.title, job.snippet, job.rationale, job.missing_skills)

    lines = ["Likely questions:"]
    for q in prep_result.likely_questions:
        lines += [f"\nQ: {q.question}", f"  Why they'll ask: {q.why_theyll_ask}", f"  How to answer: {q.how_to_answer}"]
    lines += ["\nQuestions to ask them:"] + [f"  - {q}" for q in prep_result.questions_to_ask_them]
    if prep_result.watch_out_for:
        lines += ["\nWatch out for:"] + [f"  - {w}" for w in prep_result.watch_out_for]

    notes = "\n".join(lines)
    store.update(job_id, interview_notes=notes)
    store.set_status(job_id, "interviewing")
    return jsonify({"ok": True, "interview_notes": notes, "status": "interviewing"})


@app.route("/discover", methods=["POST"])
def discover():
    """Kicks off a background discovery run and returns immediately -- the dashboard's
    JS then polls /discover/status itself to render an inline progress panel, so this
    never has to redirect anywhere."""
    with _discover_lock:
        if _discover_state["running"]:
            return jsonify({"ok": False, "info": "A discovery run is already in progress."})
        _discover_state.update(running=True, done=0, total=0, label="starting...",
                                jobs_found=0, error=None, finished=False, summary=None)

    body = request.json or {}
    engine = current_engine()
    query = body.get("query") or "product manager, product owner, business analyst"
    country = body.get("country") or "in"
    include_instahyre = bool(body.get("include_instahyre"))
    max_age_hours = body.get("max_age_hours")
    max_age_hours = int(max_age_hours) if max_age_hours else None
    min_salary = body.get("min_salary")
    min_salary = int(min_salary) if min_salary else None
    # title_keywords is the actual fix for "these roles are not matching" -- Greenhouse/
    # Lever hand back a company's whole board with no query of their own, so without this
    # every department's postings get scored against the resume. Defaults mirror the CLI's
    # --title-keywords/--exclude-title-keywords/--locations; an empty string from the form
    # disables that one filter, same convention as the CLI flags.
    title_keywords = body.get("title_keywords")
    if title_keywords is None:
        title_keywords = ("Product Manager,Technical Product Manager,TPM,Technical Product Owner,"
                           "Product Owner,Business Analyst,Platform PM,API PM")
    exclude_title_keywords = body.get("exclude_title_keywords")
    if exclude_title_keywords is None:
        exclude_title_keywords = "Intern,Senior Director,VP,Associate PM"
    locations = body.get("locations")
    if locations is None:
        locations = "Bengaluru,Bangalore,Pune,Hyderabad,Remote - India,India"
    title_keywords = [k.strip() for k in title_keywords.split(",") if k.strip()]
    exclude_title_keywords = [k.strip() for k in exclude_title_keywords.split(",") if k.strip()]
    allowed_locations = [k.strip() for k in locations.split(",") if k.strip()]

    thread = threading.Thread(
        target=_discover_worker,
        args=(engine, query, country, include_instahyre, max_age_hours, min_salary,
              title_keywords, exclude_title_keywords, allowed_locations),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True})


@app.route("/discover/status")
def discover_status():
    with _discover_lock:
        return jsonify(dict(_discover_state))


@app.route("/add-lead", methods=["GET", "POST"])
def add_lead():
    if request.method == "GET":
        return render_template("add_lead.html")

    text = request.form.get("text", "").strip()
    source = request.form.get("source", "manual")
    if not text:
        flash("Paste some text first.", "error")
        return redirect(url_for("add_lead"))

    client = get_client(current_engine())
    lead = parse_pasted_lead(client, text, source_label=source)
    if not lead.is_job_posting:
        flash("No job posting detected in that text.", "info")
        return redirect(url_for("add_lead"))

    resume_text = Path(DATA_DIR / "resume.txt").read_text()
    match = score_job_matches(client, resume_text, [lead.listing])[0]
    store = JobStore()
    job_id = store.add_match(match, source_type="manual_lead", application_method=lead.application_method,
                              application_target=lead.application_target,
                              poster_name=lead.poster_name, poster_title=lead.poster_title)
    flash(f"Added job #{job_id}: {match.job.title} [score {match.score}]", "success")
    return redirect(url_for("index", open=job_id))


@app.route("/add-browsed-page", methods=["GET", "POST"])
def add_browsed_page():
    if request.method == "GET":
        return render_template("add_browsed_page.html")

    text = request.form.get("text", "").strip()
    source_url = request.form.get("source_url", "live-browse").strip()
    if not text:
        flash("Paste some text first.", "error")
        return redirect(url_for("add_browsed_page"))

    client = get_client(current_engine())
    resume_text = Path(DATA_DIR / "resume.txt").read_text()
    listings = extract_job_listings(client, text, source_url or "live-browse")
    if not listings:
        flash("No job listings extracted from that text.", "info")
        return redirect(url_for("add_browsed_page"))

    store = JobStore()
    count = 0
    for match in score_job_matches(client, resume_text, listings):
        store.add_match(match, source_type="live_browse")
        count += 1
    flash(f"Added {count} job(s) from the browsed page.", "success")
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
def profile_view():
    store = ProfileStore()
    profile = store.load()

    if request.method == "POST":
        for field in ("full_name", "email", "phone", "location", "linkedin_url",
                      "portfolio_url", "work_authorized", "resume_path"):
            setattr(profile, field, request.form.get(field, getattr(profile, field)))
        store.save(profile)
        flash("Profile saved.", "success")
        return redirect(url_for("profile_view"))

    return render_template("profile.html", profile=profile)


if __name__ == "__main__":
    # threaded=True matters here: discovery runs on its own background thread while the
    # dashboard polls /discover/status on the main request thread -- without this the
    # dev server serves one request at a time and the poll would just queue behind it.
    app.run(debug=True, host="127.0.0.1", port=5000, threaded=True)
