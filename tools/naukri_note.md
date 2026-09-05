# Why there's no naukri_scraper.py

You said "go all in" for Naukri too, and I nearly built this the same way as
`instahyre_scraper.py` -- but while researching page structure to build it
properly, I checked Naukri's `robots.txt` directly and it explicitly
disallows automated access to their pages for the crawler I tested with.

That's a meaningfully different signal than "LinkedIn has aggressive
detection" or "Instahyre has no known enforcement history." `robots.txt` is
the internet's standard, deliberate mechanism for a site to say "please
don't automate against this" -- it's not a vague ToS clause buried in legal
text, it's a file Naukri's own engineers maintain specifically to tell bots
what not to touch. Instahyre's `robots.txt`, by contrast, has no disallow
rules at all.

I didn't want to quietly build this anyway and let the distinction get
lost in a folder of similar-looking scraper files, so I stopped and I'm
flagging it here instead. Two ways to get Naukri coverage that don't cross
this line:

1. **Naukri's own job-alert emails.** Set up a saved search on naukri.com
   with email alerts on, then paste the alert emails into `add-lead` (see
   the main README) -- same mechanism already built for LinkedIn posts and
   agency emails.
2. **If you still want an automated scraper anyway**, tell me directly and
   I'll build `naukri_scraper.py` to the same standard as the Instahyre one
   -- this is a judgment call I'm surfacing for you to make with the actual
   evidence in hand, not a refusal.
