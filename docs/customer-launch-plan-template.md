<!--
================================================================================
OPERATOR NOTES  —  read, then DELETE this whole HTML comment before export.
================================================================================

WHAT THIS IS
  A static, reusable Customer Launch Plan for the existing EUR 29.90 offer
  ("Customer Launch Plan" / candidate `ask-hn-how-do-you-find-your-first-paying-customers`,
  positioning: "A personalized strategy to help you find your first paying customers.",
  delivery: "Delivered as a personalized PDF document within 3 business days.").
  You personalise this file by hand after a sale. No LLM, no API, no web
  research, no cost.

THE BODY BELOW MATCHES `deliverable.render_launch_plan_md` SECTION-FOR-SECTION
  1 Business & product analysis   2 Ideal customer profile
  3 Customer acquisition opportunities (5-10)   4 Prioritised acquisition strategy
  5 14-day action plan (days 1..14)   6 Ready-to-use templates (2-3)
  7 Next steps
  ...so you can either ship the filled Markdown straight to PDF, or transcribe
  the sections into an intake `plan` dict and use the normal plan-* flow.

PLACEHOLDER  ->  INTAKE FIELD (from intake.INTAKE_FIELDS; buyer's own words)
  [CUSTOMER_NAME]        <- name                ("Your name")
  [BUSINESS_NAME]        <- business            ("Business name / website")
  [WEBSITE]              <- business            (the URL part, if given)
  [WHAT_YOU_SELL]        <- sells               ("What you sell")
  [CURRENT_PRICE]        <- current_price       ("Current price")
  [TARGET_CUSTOMER]      <- target_audience     ("Target audience")
  [CUSTOMER_SITUATION]   <- customer_situation  ("Current customer situation")
  [PREVIOUS_ATTEMPTS]    <- previous_attempts   ("Previous customer-acquisition attempts")
  [BIGGEST_PROBLEM]      <- biggest_problem     ("Biggest customer-acquisition problem")
  [ORDER_ID]             <- order_id
  [DATE]                 <- today's date

SPOTS THAT NEED YOUR JUDGEMENT are marked  (!)  in the body. At each one,
adapt the generic text to this buyer using only their intake answers.

RULES
  - Never invent the buyer's traction, revenue, customers, or testimonials.
  - Never write "guarantee / guaranteed / guarantees" anywhere in sections 1-7
    (keeps the plan clean if you run it through `plan-approve` QC). The one
    sanctioned disclaimer line under the title is allowed and must stay.
  - If a required intake field is blank, do NOT guess: reply to the buyer's
    order email and ask before finishing the plan.

FULFILMENT FLOW (matches the repo; run from the project root, PYTHONPATH=src)
  1. Payment shows in PayPal:
       revenue_os paypal-sync                      # books paypal:<capture> into revenue.json
  2. Buyer submitted the post-payment form; export it and import:
       revenue_os intake-import <export.csv|json>  # stores only if capture matches a booked payment
       revenue_os intake-list
  3. Human review gate:
       revenue_os intake-review <ORDER_ID> --actor <you>
  4. Personalise THIS file: replace every [PLACEHOLDER], resolve every (!),
     delete these operator notes.
  5. Produce the PDF, either:
     (a) System path - transcribe the filled sections into the intake `plan`
         (IntakeStore.attach_plan), then:
           revenue_os plan-approve <ORDER_ID> --actor <you>
           revenue_os plan-deliver <ORDER_ID>            # stages the PDF (pdf.render_markdown_pdf)
           revenue_os plan-deliver <ORDER_ID> --send     # emails it via SMTP_* in .env
     (b) Fast manual path - save the filled Markdown and convert with pandoc
         or print-to-PDF, then email it yourself from BUSINESS_EMAIL
         (divdav12support@gmail.com).
  6. Keep the two promises already on the checkout page:
       - "Delivered as a personalized PDF document within 3 business days."
       - "If the plan cannot be delivered, the payment is refunded in full via PayPal."
  7. Record the sale outcome once the plan is delivered (experiment ledger picks
     up the paid+intake link automatically via `revenue-loop` / `experiments`).

TIME BUDGET: ~30-45 minutes per buyer once you know the template.
================================================================================
-->

# Customer Launch Plan

Prepared for **[CUSTOMER_NAME]** ([BUSINESS_NAME]).

_This is a personalised research and strategy document. It is not a guarantee of customers, revenue, or results._

You bought research and a personalised strategy — a plan you carry out yourself, not a promise of customers or revenue. This document turns what you told us about [BUSINESS_NAME] into a concrete 14-day sequence: who to reach, where, in what order, and exactly what to send.

---

## 1. Business & product analysis

- **What you sell:** [WHAT_YOU_SELL] — currently priced at [CURRENT_PRICE].
- **Problem it solves:** (!) In one sentence, state the specific, expensive-or-annoying problem [WHAT_YOU_SELL] removes for [TARGET_CUSTOMER]. Base this only on the buyer's "What you sell" and "Target audience" answers. Avoid feature lists; name the outcome the customer gets.
- **Core value proposition:** (!) Complete this sentence for [BUSINESS_NAME]: "For [TARGET_CUSTOMER] who [situation], [WHAT_YOU_SELL] is the [category] that [single clearest benefit], unlike [the current alternative they use — often 'doing it manually' or 'a bigger, heavier tool']."

**Positioning statement to use everywhere (site, bio, outreach):**
> [BUSINESS_NAME] helps [TARGET_CUSTOMER] [achieve the core outcome] without [the main pain of the alternative].

(!) Rewrite the [WEBSITE] hero headline and subheadline to match this exact statement before you start outreach — every visitor you send there this fortnight should land on copy that says who it is for and what changes.

---

## 2. Ideal customer profile

- **Most likely first customer:** (!) From [TARGET_CUSTOMER] and [CUSTOMER_SITUATION], describe the *single* best-fit buyer — not the whole market. The first customers of a new offer are almost always the people who feel [BIGGEST_PROBLEM] most acutely *right now* and have the authority to say yes without approval.
- **Relevant characteristics:** (!) List 4–6 observable traits you can actually filter on: role/title, company size or stage, the tool or workaround they use today, a trigger event that makes them look for [WHAT_YOU_SELL], and where they have public presence.
- **Where to reach them:** (!) Name the 3–5 specific places this person already gathers and talks about [BIGGEST_PROBLEM] — a named subreddit or forum, a Slack/Discord community, a newsletter's comment section, a marketplace category, a recurring Twitter/LinkedIn conversation, a local or online meetup. "Social media" is not an answer; a named community is.

**How to confirm this profile (do this on Day 1–2):** write the profile down, then find one real person who matches it and read 10 of their recent public posts. If they are not talking about [BIGGEST_PROBLEM] or its symptoms, tighten the profile before spending any outreach effort.

---

## 3. Customer acquisition opportunities

Seven concrete channels, ordered roughly by how fast they produce real conversations for a new offer. (!) Keep the 5–7 that genuinely fit [BUSINESS_NAME]; cut any that clearly do not; adapt every "why it fits you" and "first step" line to the buyer's own answers.

### 1. Your existing network and past contacts
- **Channel:** direct 1:1 — email, DM, or a message to people who already know you or the problem.
- **Why it fits you:** this is the warmest possible audience and the fastest to a first paying customer; [PREVIOUS_ATTEMPTS] shows what these people have already heard from you, so you can pick up the thread rather than start cold.
- **First step:** list 15 people who have either seen [WHAT_YOU_SELL] or clearly have [BIGGEST_PROBLEM]. Message 5 of them today asking for a reaction to the new positioning — not for a sale.

### 2. One focused community where [TARGET_CUSTOMER] asks for help
- **Channel:** the single best-fit forum / subreddit / Discord / Slack from section 2's "where to reach them".
- **Why it fits you:** members post the exact problem [WHAT_YOU_SELL] solves, in public, with buying intent; a genuinely useful answer builds standing you can convert later.
- **First step:** join, read the self-promotion rules, and this week answer 3 questions with real substance and **no link**. Note every thread where someone describes [BIGGEST_PROBLEM].

### 3. Direct outreach to a named prospect list
- **Channel:** cold email / LinkedIn / platform DM to people who match the ideal customer profile.
- **Why it fits you:** you control the targeting and the volume; 10–20 personalised messages a day is sustainable solo and predictable.
- **First step:** build a 30-row list (name, link, one line on why they fit) by Day 3, then send Template A to the first 10.

### 4. Content on a channel you own
- **Channel:** your newsletter, X/LinkedIn, or blog — "building [BUSINESS_NAME] in public".
- **Why it fits you:** it compounds over the fortnight and makes week-2 outreach land warmer because prospects can see you are active and credible.
- **First step:** publish the Template B post today and commit to two posts a week for the plan window.

### 5. A launch / "Show" post
- **Channel:** a one-time post where launches are on-topic — Show HN, r/SideProject, Indie Hackers, a relevant launch directory (!) (name the 1–2 that fit [WHAT_YOU_SELL]).
- **Why it fits you:** a single concentrated burst of on-topic traffic and blunt feedback, at zero cost.
- **First step:** draft a plain, non-hyped post that leads with the problem and what you built; schedule it for a weekday morning in the plan window.

### 6. Partnerships with adjacent, non-competing products or creators
- **Channel:** 1:1 outreach to 5 businesses or creators who already serve [TARGET_CUSTOMER] without competing with [WHAT_YOU_SELL].
- **Why it fits you:** they have already assembled the audience you are trying to reach; a small swap is cheaper than building reach from scratch.
- **First step:** list 5 complementary names and send each one a specific proposal — a mutual mention, a bundle, or a guest post.

### 7. Niche directories and marketplaces
- **Channel:** category directories, marketplace listings, and "best [category] for [audience]" roundups relevant to [WHAT_YOU_SELL].
- **Why it fits you:** low-effort and durable; the traffic is small but has real intent and keeps arriving after the plan window.
- **First step:** find 5 directories that list products like yours and submit to all 5 this week.

---

## 4. Prioritised acquisition strategy

**Ranking (default — reorder for [BUSINESS_NAME] based on [BIGGEST_PROBLEM] and where [TARGET_CUSTOMER] actually is):**

1. Existing network and past contacts
2. One focused community
3. Direct outreach to a named prospect list
4. Content on a channel you own
5. A launch / "Show" post
6. Partnerships with adjacent products or creators
7. Niche directories and marketplaces

**Start with:** the network and the one community, running in parallel from Day 1.

**Reasoning:** the first customers of a new offer come from conversations, not from reach. The network gives you the fastest honest feedback and the shortest path to a first "yes"; the community puts you in front of people describing [BIGGEST_PROBLEM] with intent. Direct outreach scales those conversations from Day 7, while the owned-content channel builds quietly so that by week 2 the people you message have already seen [BUSINESS_NAME]. Launch posts, partnerships, and directories are worthwhile but slower or more one-off, so they come after the conversation channels are running.

---

## 5. 14-day action plan

Work in focused blocks; 60–120 minutes a day is enough if it is consistent. Every link you share this fortnight gets a `?ref=` or UTM tag so you know what worked.

**Day 1 — Positioning**
Write the one-sentence positioning statement and the "who it's for / what changes / why now" from section 1. Rewrite the [WEBSITE] hero headline and subheadline to match. Update your bios on the channels you will use.

**Day 2 — Ideal customer profile**
Write the full profile from section 2. Find one real person who matches it, read 10 of their recent posts, and tighten the profile. List the 3–5 named places they gather.

**Day 3 — Build the prospect list**
Create a spreadsheet of 30 named people or companies that match the profile: name, link, one line on why they fit. Do not message anyone yet.

**Day 4 — Set up tracking**
Make a simple sheet: date, channel, person, what you sent, reply (y/n), outcome. Add `?ref=` tags to every link you plan to share. Draft Templates A, B and C (section 6) with [BUSINESS_NAME]'s details filled in.

**Day 5 — Warm outreach**
Message 10 people from your existing network (channel 1). Reference [PREVIOUS_ATTEMPTS] where relevant. Ask for a reaction to the new positioning and whether they know anyone with [BIGGEST_PROBLEM]. No pitch.

**Day 6 — Community: help only**
In your one primary community, answer 3 questions with genuinely useful, link-free replies. Save every thread where someone describes [BIGGEST_PROBLEM]; you will follow up with those people later.

**Day 7 — First outreach batch**
Send 10 personalised Template A messages to the top of your prospect list. Log each one. Goal: start a conversation and offer help, not close a sale.

**Day 8 — First content post**
Publish the Template B post on your owned channel. Share it in the one community where it is on-topic and allowed by the rules. Reply to every comment.

**Day 9 — Follow up + new batch**
Send Template C to everyone from Day 5 and Day 7 who did not reply. Send 10 new Template A messages to the next prospects on the list.

**Day 10 — Conversations**
Have 3 real conversations (calls or DM threads) with anyone who engaged. Ask the four discovery questions in the appendix. Write down their answers in their own words.

**Day 11 — Improve + outreach**
Make one small, concrete improvement to [WHAT_YOU_SELL] or [WEBSITE] based on Day 10. Send 10 new Template A messages.

**Day 12 — Second content post + reshare**
Turn one Day 10 conversation into a short, useful post (no names or quotes without permission). Reshare the Day 8 post in a second on-topic place. If a launch post fits [BUSINESS_NAME], post it today.

**Day 13 — Ask directly**
Follow up every open thread. Ask the people who engaged whether [WHAT_YOU_SELL] at [CURRENT_PRICE] is something they want now. If yes, send the link. If no, ask what would need to be true.

**Day 14 — Review and plan the next fortnight**
Read the tracking sheet. Which channel produced replies and conversations? Write the next 14 days: drop what produced nothing, double down on the channel that worked, keep the two-posts-a-week cadence.

---

## 6. Ready-to-use templates

Fill the bracketed parts. Keep them short. Never claim results you have not had.

### Template A — First 1:1 message
_For a named prospect from your list who fits the ideal customer profile. Lead with them, not with [BUSINESS_NAME]._

```
Hi [First name],

I saw [specific thing they posted / built / said] — [one sincere, specific sentence about it].

I'm working on [WHAT_YOU_SELL] for [TARGET_CUSTOMER] dealing with [BIGGEST_PROBLEM].
I'm not trying to sell you anything today — I'd genuinely value 5 minutes of your
take on whether [the problem framing] matches your experience.

Worth a quick reply either way. Thanks for reading.
[Your name] — [WEBSITE]
```

### Template B — Community / build-in-public post
_Useful on its own; works as a progress update where self-promotion is allowed._

```
[Plain-language title about the problem, not the product]

For the last while I've been talking to [TARGET_CUSTOMER] about [BIGGEST_PROBLEM].
The pattern I keep hearing: [one concrete, non-obvious observation from real
conversations — or from your own experience if you have not had the conversations yet].

Here's the approach I'd take: [2–4 concrete steps someone could act on today,
whether or not they ever use [WHAT_YOU_SELL]].

I'm building [BUSINESS_NAME] around this ([WEBSITE]). Happy to go deeper in the
comments if it's useful.
```

### Template C — Follow-up
_3–4 days after no reply. One nudge, then stop._

```
Hi [First name],

Following up once in case this got buried. Same question — does [the problem
framing] match what you see with [TARGET_CUSTOMER]?

If it's not relevant, no problem at all and I won't chase it further.
[Your name]
```

---

## 7. Next steps

- [ ] Confirm the Day 1 positioning statement with one person who fits [TARGET_CUSTOMER].
- [ ] Fill the prospect list to 30 rows before sending any outreach.
- [ ] Send the first 10 Template A messages by Day 7 and log every one.
- [ ] Publish the first content post by Day 8.
- [ ] Put the Day 14 tracking-sheet review in your calendar now.
- [ ] Reply to order [ORDER_ID] if any part of this plan needs adjusting to [BUSINESS_NAME].

---

## Appendix — four discovery questions

Ask these in every real conversation and write the answers verbatim:

1. When did you last run into [BIGGEST_PROBLEM], and what did you do about it?
2. What are you using today instead — even if it is a spreadsheet or doing it by hand?
3. If that stopped working tomorrow, what would break for you?
4. Who else on your team or in your circle deals with this?

---

_Basis: personalised by the operator from the AI-Revenue-OS static Customer Launch Plan template (v1). Prepared [DATE]. Method: manual personalisation from your intake answers — no automated web research. Delivered as a PDF by email. If the plan cannot be delivered, the payment is refunded in full via PayPal._
