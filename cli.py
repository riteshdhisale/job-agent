"""
job_agent CLI -- the single entry point for your whole job-search system.

    python cli.py discover                     # Agent 1: scan career sites + Adzuna + RemoteOK
    python cli.py add-lead --file lead.txt      # LinkedIn post / portal email alert / agency message
    python cli.py list [--status STATUS]        # browse what's been found
    python cli.py show ID                       # full detail on one job
    python cli.py tailor ID                     # Agent 2: tailor resume (only if score >= 90)
    python cli.py contacts ID --company "Name"  # Agent 3a: find people to reach out to
    python cli.py outreach ID                   # Agent 3b: draft a personalized message
    python cli.py apply ID                      # Agent 4: fill the form / draft the email -- never sends
    python cli.py mark-applied ID               # you confirm YOU actually submitted it
    python cli.py prep ID                       # Agent 5: interview prep, once shortlisted
    python cli.py profile show|set KEY VALUE    # manage your Candidate Profile
    python cli.py delete ID (or --all)          # remove a job, or wipe everything and start fresh

Every command takes --engine fake (default, no API key, crude but free),
--engine claude (real quality, needs ANTHROPIC_API_KEY, pay-as-you-go billing), or
--engine gemini (real quality, needs GEMINI_API_KEY, has a genuine free tier -- see
https://aistudio.google.com/apikey; note a personal Gemini Pro/Advanced app
subscription does NOT include this on its own). Start with fake to see the whole
system wired together, then switch to whichever real engine fits your budget.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
from tools.score_tool import score_job_matches
from tools.extract_tool import extract_job_listings
from tools.instahyre_scraper import fetch_instahyre_listings

DATA_DIR = Path(__file__).resolve().parent / "data"


_ENGINES = {"fake": FakeLLMClient, "claude": AnthropicClient, "gemini": GeminiClient}


def get_client(engine: str):
    cls = _ENGINES.get(engine)
    if cls is None:
        raise ValueError(f"Unknown engine '{engine}' -- choose one of {sorted(_ENGINES)}")
    return cls()


def add_engine_arg(p):
    p.add_argument("--engine", choices=["fake", "claude", "gemini"], default="fake")


def cmd_discover(args):
    client = get_client(args.engine)
    resume_text = Path(args.resume).read_text()
    site_urls = json.loads(Path(args.sites).read_text())
    greenhouse_boards = json.loads(Path(args.greenhouse_boards).read_text()) if Path(args.greenhouse_boards).exists() else []
    lever_sites = json.loads(Path(args.lever_sites).read_text()) if Path(args.lever_sites).exists() else []
    title_keywords = [k.strip() for k in args.title_keywords.split(",") if k.strip()]
    exclude_title_keywords = [k.strip() for k in args.exclude_title_keywords.split(",") if k.strip()]
    allowed_locations = [k.strip() for k in args.locations.split(",") if k.strip()]
    store = JobStore()

    report = run_discovery(client, resume_text, site_urls, store, query=args.query, country=args.country,
                            use_adzuna=not args.no_adzuna, use_remoteok=not args.no_remoteok,
                            greenhouse_boards=greenhouse_boards, lever_sites=lever_sites,
                            max_age_hours=args.max_age_hours, min_salary=args.min_salary,
                            title_keywords=title_keywords, exclude_title_keywords=exclude_title_keywords,
                            allowed_locations=allowed_locations)

    jobs_found = report.jobs_found
    if args.include_instahyre:
        # Instahyre's scraper takes one search term, not several -- if --query has
        # multiple comma-separated roles, only search the first one there. The other
        # roles still get the full treatment via Adzuna/RemoteOK/career sites above.
        instahyre_query = args.query.split(",")[0].strip()
        instahyre = fetch_instahyre_listings(query=instahyre_query, force=args.force_scrape)
        if not instahyre.ok:
            report.sites_failed.append(f"instahyre -- {instahyre.note}")
        elif instahyre.listings:
            for match in score_job_matches(client, resume_text, instahyre.listings):
                store.add_match(match, source_type="scraped")
                jobs_found += 1

    print(f"Career sites attempted: {report.sites_attempted}")
    print(f"Total jobs found and scored across all sources: {jobs_found}")
    if report.sites_failed:
        print("\nSkipped / failed:")
        for f in report.sites_failed:
            print(f"  - {f}")
    print("\nRun `python cli.py list` to see everything, ranked by score.")


def cmd_add_browsed_page(args):
    """Ingest a page's text you (or a live Claude-in-Chrome browsing session
    with you) just read -- e.g. a LinkedIn Jobs search results page. This is
    the interactive, human-paced path decided on for LinkedIn: no scraper
    runs unattended, this just accepts whatever text was read during an
    actual session and scores it exactly like any other source."""
    client = get_client(args.engine)
    text = Path(args.file).read_text() if args.file else args.text
    if not text:
        print("Provide --file PATH or --text '...'", file=sys.stderr)
        sys.exit(1)

    resume_text = Path(args.resume).read_text()
    listings = extract_job_listings(client, text, args.source_url or "live-browse")
    if not listings:
        print("No job listings extracted from that text.")
        return

    store = JobStore()
    count = 0
    for match in score_job_matches(client, resume_text, listings):
        store.add_match(match, source_type="live_browse")
        count += 1
    print(f"Added {count} job(s) from the browsed page. Run `python cli.py list` to see them.")


def cmd_add_lead(args):
    client = get_client(args.engine)
    text = Path(args.file).read_text() if args.file else args.text
    if not text:
        print("Provide --file PATH or --text '...'", file=sys.stderr)
        sys.exit(1)

    lead = parse_pasted_lead(client, text, source_label=args.source)
    if not lead.is_job_posting:
        print("No job posting detected in that text -- nothing added.")
        return

    resume_text = Path(args.resume).read_text()
    match = score_job_matches(client, resume_text, [lead.listing])[0]
    store = JobStore()
    job_id = store.add_match(match, source_type="manual_lead", application_method=lead.application_method,
                              application_target=lead.application_target,
                              poster_name=lead.poster_name, poster_title=lead.poster_title)
    print(f"Added job #{job_id}: {match.job.title} at {lead.company or '(company unknown)'}  [score {match.score}]")
    print(f"  apply via: {lead.application_method} -> {lead.application_target or '(not specified)'}")
    if lead.poster_name:
        print(f"  contact already known: {lead.poster_name} ({lead.poster_title})")


def _format_lakhs(value):
    if value is None:
        return None
    text = f"{value / 100000:.1f}".rstrip("0").rstrip(".")
    return f"₹{text}L"


def _format_salary(job) -> str:
    if job.salary_min and job.salary_max and job.salary_min != job.salary_max:
        return f"{_format_lakhs(job.salary_min)}-{_format_lakhs(job.salary_max)}"
    if job.salary_max:
        return _format_lakhs(job.salary_max)
    if job.salary_min:
        return _format_lakhs(job.salary_min)
    return "not disclosed"


def cmd_list(args):
    store = JobStore()
    jobs = store.list(status=args.status)
    if not jobs:
        print("No jobs found yet. Run `python cli.py discover` or `add-lead` first.")
        return
    for j in jobs[: args.top]:
        extra = ""
        if j.salary_min or j.salary_max:
            extra += f"  {_format_salary(j)}"
        if j.posted_at:
            extra += f"  posted {j.posted_at[:10]}"
        print(f"#{j.id:<4} [{j.score:>3}] {j.status:<14} {j.title}  ({j.source_type}){extra}")


def cmd_show(args):
    store = JobStore()
    j = store.get(args.job_id)
    if not j:
        print(f"No job #{args.job_id}", file=sys.stderr)
        sys.exit(1)
    print(f"#{j.id} {j.title}  ({j.location})")
    print(f"Status: {j.status} | Source: {j.source_type} | Apply via: {j.application_method} -> {j.application_target or j.url}")
    print(f"Salary: {_format_salary(j)} | Posted: {j.posted_at or 'unknown'}")
    print(f"Score: {j.score}\nRationale: {j.rationale}")
    print(f"Matched skills: {', '.join(j.matched_skills) or 'none'}")
    print(f"Missing skills: {', '.join(j.missing_skills) or 'none'}")
    if j.tailored_resume:
        print(f"\n--- Tailored resume ---\n{j.tailored_resume}")
    if j.outreach_contacts:
        print(f"\n--- Contacts ---\n{json.dumps(j.outreach_contacts, indent=2)}")
    if j.outreach_message:
        print(f"\n--- Outreach draft ---\n{j.outreach_message}")
    if j.application_notes:
        print(f"\n--- Application notes ---\n{j.application_notes}")
    if j.interview_notes:
        print(f"\n--- Interview prep ---\n{j.interview_notes}")


def cmd_tailor(args):
    client = get_client(args.engine)
    store = JobStore()
    j = store.get(args.job_id)
    if not j:
        print(f"No job #{args.job_id}", file=sys.stderr)
        sys.exit(1)

    resume_text = Path(args.resume).read_text()
    try:
        result = tailor_resume(client, resume_text, j.title, j.snippet, j.score, force=args.force)
    except MatchScoreTooLowError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    bullets_text = "\n".join(f"- {b.tailored}  [{b.reason}]" for b in result.tailored_bullets)
    tailored_text = f"{result.tailored_summary}\n\n{bullets_text}"
    if result.flagged_gaps:
        tailored_text += "\n\nFlagged gaps (be ready to address honestly):\n" + "\n".join(f"- {g}" for g in result.flagged_gaps)

    store.update(args.job_id, tailored_resume=tailored_text)
    store.set_status(args.job_id, "tailored")
    print(tailored_text)


def cmd_contacts(args):
    store = JobStore()
    j = store.get(args.job_id)
    if not j:
        print(f"No job #{args.job_id}", file=sys.stderr)
        sys.exit(1)

    if j.outreach_contacts:
        print("Already have contact(s) for this job (e.g. from a pasted LinkedIn post):")
        print(json.dumps(j.outreach_contacts, indent=2))
        return

    if not args.company:
        print("No company on file for this job -- pass --company \"Name\" to look up contacts.", file=sys.stderr)
        sys.exit(1)

    contacts = find_contacts(args.company, department_hint="product")
    if not contacts:
        print("No contacts found (no APOLLO_API_KEY set, or nothing matched). "
              "You can still draft a message manually with `outreach --name/--title`.")
        return

    contacts_json = [{"name": c.name, "title": c.title, "linkedin_url": c.linkedin_url,
                       "email": c.email, "source": c.source} for c in contacts]
    store.update(args.job_id, outreach_contacts=json.dumps(contacts_json))
    print(json.dumps(contacts_json, indent=2))


def cmd_outreach(args):
    client = get_client(args.engine)
    store = JobStore()
    j = store.get(args.job_id)
    if not j:
        print(f"No job #{args.job_id}", file=sys.stderr)
        sys.exit(1)

    resume_text = Path(args.resume).read_text()
    if args.name:
        recipient_name, recipient_title = args.name, args.title or ""
    elif j.outreach_contacts:
        top = j.outreach_contacts[0]
        recipient_name, recipient_title = top.get("name", ""), top.get("title", "")
    else:
        print("No contact on file -- run `contacts` first, or pass --name/--title.", file=sys.stderr)
        sys.exit(1)

    draft = draft_outreach_message(client, resume_text, j.title, args.company or "the company",
                                    j.rationale, recipient_name, recipient_title)
    message_text = f"To: {draft.recipient_name} ({draft.recipient_title})\nSubject: {draft.subject_line}\n\n{draft.message}"
    store.update(args.job_id, outreach_message=message_text)
    store.set_status(args.job_id, "contacted")
    print(message_text)
    print("\n[This is a DRAFT. Review it, then send it yourself -- this system never sends anything.]")


def cmd_apply(args):
    client = get_client(args.engine)
    store = JobStore()
    profile_store = ProfileStore()
    profile = profile_store.load()

    j = store.get(args.job_id)
    if not j:
        print(f"No job #{args.job_id}", file=sys.stderr)
        sys.exit(1)

    if j.application_method == "email":
        draft = draft_application_email(client, j.title, args.company or "the company",
                                         j.rationale, j.application_target or "")
        notes = f"To: {draft.to}\nSubject: {draft.subject}\n\n{draft.body}"
        store.update(args.job_id, application_notes=notes)
        store.set_status(args.job_id, "ready_to_submit")
        print(notes)
        print("\n[This is a DRAFT email. Attach your resume and send it yourself.]")
        return

    if j.application_method in ("dm", "unclear"):
        print(f"This job's application method is '{j.application_method}' -- "
              f"target: {j.application_target or '(not specified)'}.")
        print("No safe automation for this (e.g. an unsolicited LinkedIn DM) -- handle it yourself, "
              "then run `mark-applied` once you have.")
        return

    # 'web_form' or 'link' -- try the Playwright form-filler.
    url = j.application_target or j.url
    screenshot_path = str(DATA_DIR / f"apply_screenshot_job{args.job_id}.png")
    result = fill_application_form(url, profile, screenshot_path=screenshot_path, headless=not args.show_browser)

    print(f"Filled {len(result.filled)} field(s) automatically from your profile:")
    for f in result.filled:
        print(f"  - {f.label}: {f.value}  ({f.source})")

    if result.unmatched_fields:
        print(f"\n{len(result.unmatched_fields)} field(s) need your answer (will be saved for next time):")
        for label in result.unmatched_fields:
            answer = input(f"  {label}: ")
            profile.remember(label, answer)
            # NOTE: re-filling the now-answered field on the live page is left
            # for you to do in the browser/screenshot review step -- this
            # command's job is to never guess and to make sure you're never
            # asked the same question twice again, not to force a second
            # automated pass.
        profile_store.save(profile)
        print("Saved your answers to the Candidate Profile -- you won't be asked these again.")

    notes = (f"Filled: {[f.label for f in result.filled]}\n"
             f"Answered and saved: {result.unmatched_fields}\n"
             f"Screenshot: {result.screenshot_path}")
    store.update(args.job_id, application_notes=notes)
    store.set_status(args.job_id, "ready_to_submit")
    print(f"\nScreenshot saved to {result.screenshot_path}. Review it, then submit yourself -- "
          f"this system never clicks submit.")


def cmd_mark_applied(args):
    store = JobStore()
    store.set_status(args.job_id, "applied")
    print(f"Job #{args.job_id} marked as applied.")


def cmd_delete(args):
    store = JobStore()
    if args.all:
        n = store.delete_all()
        print(f"Deleted {n} job(s). Your job list is now empty.")
        return
    if args.job_id is None:
        print("Pass a job ID, or --all to wipe every job.", file=sys.stderr)
        sys.exit(1)
    if store.delete(args.job_id):
        print(f"Job #{args.job_id} deleted.")
    else:
        print(f"No job #{args.job_id} found.", file=sys.stderr)
        sys.exit(1)


def cmd_prep(args):
    client = get_client(args.engine)
    store = JobStore()
    j = store.get(args.job_id)
    if not j:
        print(f"No job #{args.job_id}", file=sys.stderr)
        sys.exit(1)

    prep = prepare_for_interview(client, j.tailored_resume or j.rationale, j.title, j.snippet,
                                  j.rationale, j.missing_skills)

    lines = ["Likely questions:"]
    for q in prep.likely_questions:
        lines += [f"\nQ: {q.question}", f"  Why they'll ask: {q.why_theyll_ask}", f"  How to answer: {q.how_to_answer}"]
    lines += ["\nQuestions to ask them:"] + [f"  - {q}" for q in prep.questions_to_ask_them]
    if prep.watch_out_for:
        lines += ["\nWatch out for (prepare an honest answer):"] + [f"  - {w}" for w in prep.watch_out_for]

    notes = "\n".join(lines)
    store.update(args.job_id, interview_notes=notes)
    store.set_status(args.job_id, "interviewing")
    print(notes)


def cmd_profile(args):
    store = ProfileStore()
    profile = store.load()
    if args.profile_action == "show":
        print(json.dumps(profile.__dict__, indent=2))
    elif args.profile_action == "set":
        if not hasattr(profile, args.key):
            print(f"Unknown profile field '{args.key}'. Fields: full_name, email, phone, location, "
                  f"linkedin_url, portfolio_url, work_authorized, resume_path", file=sys.stderr)
            sys.exit(1)
        setattr(profile, args.key, args.value)
        store.save(profile)
        print(f"Saved {args.key} = {args.value}")


def build_parser():
    p = argparse.ArgumentParser(description="Your personal job-search agent system.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Agent 1: scan career sites + Adzuna + RemoteOK")
    d.add_argument("--resume", default=str(DATA_DIR / "resume.txt"))
    d.add_argument("--sites", default=str(DATA_DIR / "career_sites.json"))
    d.add_argument("--query", default="product manager, product owner, business analyst",
                    help="Comma-separated job titles to search for (Adzuna + RemoteOK's own query "
                         "params). See --title-keywords for the separate, source-agnostic filter "
                         "that also applies to career sites/Greenhouse/Lever.")
    d.add_argument("--country", default="in")
    d.add_argument("--no-adzuna", action="store_true")
    d.add_argument("--no-remoteok", action="store_true")
    d.add_argument("--max-age-hours", type=int, default=None,
                    help="Only keep listings posted within this many hours (Adzuna only has "
                         "real posting-date data today -- other sources' listings pass through "
                         "unfiltered rather than being dropped for lacking a date).")
    d.add_argument("--min-salary", type=int, default=None,
                    help="Only keep listings whose disclosed salary meets this floor, in the "
                         "source's local currency (e.g. 3000000 for 30 LPA in India). Listings "
                         "that don't disclose a salary at all are kept, not excluded.")
    d.add_argument("--greenhouse-boards", default=str(DATA_DIR / "greenhouse_boards.json"))
    d.add_argument("--lever-sites", default=str(DATA_DIR / "lever_sites.json"))
    d.add_argument("--title-keywords",
                    default="Product Manager,Technical Product Manager,TPM,Technical Product Owner,"
                            "Product Owner,Business Analyst,Platform PM,API PM",
                    help="Comma-separated; a listing's TITLE must contain at least one of these "
                         "(case-insensitive substring) to be scored, on every source including "
                         "Greenhouse/Lever -- which, unlike Adzuna/RemoteOK, have no query of their "
                         "own and otherwise hand back a company's entire job board. This is the fix "
                         "for 'these roles are not matching': it's the actual off-topic-role filter, "
                         "not --query. Pass an empty string to disable and score everything found.")
    d.add_argument("--exclude-title-keywords", default="Intern,Senior Director,VP,Associate PM",
                    help="Comma-separated; a listing whose title contains any of these is dropped "
                         "even if it matched --title-keywords. Pass an empty string to disable.")
    d.add_argument("--locations", default="Bengaluru,Bangalore,Pune,Hyderabad,Remote - India,India",
                    help="Comma-separated; a listing with a disclosed location matching none of "
                         "these is dropped (a blank/unknown location always passes -- see "
                         "tools/listing_filters.py). Pass an empty string to disable.")
    d.add_argument("--include-instahyre", action="store_true",
                    help="Opt-in: scrapes Instahyre's search results (rate-limited to once per cooldown window)")
    d.add_argument("--force-scrape", action="store_true", help="Override Instahyre's cooldown just this once")
    add_engine_arg(d)
    d.set_defaults(func=cmd_discover)

    ab = sub.add_parser("add-browsed-page", help="Ingest text from a page you/Claude just read live (e.g. LinkedIn Jobs)")
    ab.add_argument("--file", help="Path to a text file with the page content")
    ab.add_argument("--text", help="Paste the content directly as an argument")
    ab.add_argument("--source-url", default="", help="Where this came from, e.g. a LinkedIn search URL")
    ab.add_argument("--resume", default=str(DATA_DIR / "resume.txt"))
    add_engine_arg(ab)
    ab.set_defaults(func=cmd_add_browsed_page)

    al = sub.add_parser("add-lead", help="Add a LinkedIn post / portal alert email / agency message you pasted")
    al.add_argument("--file", help="Path to a text file with the pasted content")
    al.add_argument("--text", help="Paste the content directly as an argument")
    al.add_argument("--source", default="manual", help="e.g. linkedin_post, email_alert_naukri, agency_email")
    al.add_argument("--resume", default=str(DATA_DIR / "resume.txt"))
    add_engine_arg(al)
    al.set_defaults(func=cmd_add_lead)

    l = sub.add_parser("list", help="List discovered jobs")
    l.add_argument("--status", choices=STATUSES)
    l.add_argument("--top", type=int, default=25)
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="Show full detail on one job")
    s.add_argument("job_id", type=int)
    s.set_defaults(func=cmd_show)

    t = sub.add_parser("tailor", help="Agent 2: tailor resume (only if score >= 90)")
    t.add_argument("job_id", type=int)
    t.add_argument("--resume", default=str(DATA_DIR / "resume.txt"))
    t.add_argument("--force", action="store_true", help=f"Override the {MIN_MATCH_SCORE_TO_TAILOR}-score gate")
    add_engine_arg(t)
    t.set_defaults(func=cmd_tailor)

    c = sub.add_parser("contacts", help="Agent 3a: find people to reach out to")
    c.add_argument("job_id", type=int)
    c.add_argument("--company")
    c.set_defaults(func=cmd_contacts)

    o = sub.add_parser("outreach", help="Agent 3b: draft a personalized outreach message")
    o.add_argument("job_id", type=int)
    o.add_argument("--company")
    o.add_argument("--name", help="Override: draft to this person instead of the stored contact")
    o.add_argument("--title", help="Recipient's title, used with --name")
    o.add_argument("--resume", default=str(DATA_DIR / "resume.txt"))
    add_engine_arg(o)
    o.set_defaults(func=cmd_outreach)

    ap = sub.add_parser("apply", help="Agent 4: fill the form / draft the email -- never sends")
    ap.add_argument("job_id", type=int)
    ap.add_argument("--company")
    ap.add_argument("--show-browser", action="store_true", help="Run Playwright non-headless, to watch it work")
    add_engine_arg(ap)
    ap.set_defaults(func=cmd_apply)

    ma = sub.add_parser("mark-applied", help="Confirm YOU actually submitted this application")
    ma.add_argument("job_id", type=int)
    ma.set_defaults(func=cmd_mark_applied)

    de = sub.add_parser("delete", help="Remove a job (or --all to wipe everything and start fresh)")
    de.add_argument("job_id", type=int, nargs="?", default=None)
    de.add_argument("--all", action="store_true", help="Delete every job, not just one")
    de.set_defaults(func=cmd_delete)

    pr = sub.add_parser("prep", help="Agent 5: interview prep, once shortlisted")
    pr.add_argument("job_id", type=int)
    add_engine_arg(pr)
    pr.set_defaults(func=cmd_prep)

    pf = sub.add_parser("profile", help="Manage your Candidate Profile")
    pf_sub = pf.add_subparsers(dest="profile_action", required=True)
    pf_sub.add_parser("show")
    pf_set = pf_sub.add_parser("set")
    pf_set.add_argument("key")
    pf_set.add_argument("value")
    pf.set_defaults(func=cmd_profile)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
