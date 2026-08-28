\# AI-Revenue-OS — Claude Code Instructions



\## Mission



Build a legal, automated multi-agent AI ecosystem whose long-term goal is to discover, build, launch, measure, and improve legitimate revenue-generating opportunities.



The human owner must remain in control of money and legally sensitive actions.



\## Core Principles



1\. Build incrementally. Never attempt to build the entire system at once.

2\. Prefer simple, reliable solutions over unnecessary complexity.

3\. Minimize token usage, API usage, compute, and development time.

4\. Do not create code, files, agents, abstractions, or dependencies unless they are currently needed.

5\. Reuse existing components before creating new ones.

6\. Keep the architecture modular so agents and tools can be added later.

7\. Never invent functionality that has not been implemented.

8\. Never claim something works without testing it.

9\. Before making major architectural changes, explain the change briefly and wait for confirmation.

10\. For small, safe implementation tasks, proceed without unnecessary questions.



\## Token Efficiency



\- Keep responses concise and directly relevant.

\- Do not repeat information already known from project files.

\- Inspect only the files necessary for the current task.

\- Do not repeatedly reread unchanged files.

\- Do not perform unnecessary research.

\- Do not generate large amounts of code unless required.

\- Prefer targeted edits over rewriting entire files.

\- Avoid unnecessary tests, builds, refactors, or verification steps.

\- When a task is complete, report only:

&#x20; - what changed

&#x20; - what was tested

&#x20; - any remaining issue



\## Development Workflow



For each task:



1\. Understand the requested change.

2\. Inspect the minimum necessary files.

3\. State a short implementation plan if the task is non-trivial.

4\. Implement the smallest correct change.

5\. Run only relevant tests/checks.

6\. Report the result concisely.



Do not continue building unrelated features after completing the requested task.



\## Architecture



The system will eventually contain:



\- CEO / Orchestrator

\- Specialized AI agents

\- Task queue

\- Shared memory

\- Database

\- Tool system

\- Event system

\- Live dashboard

\- Cost controller

\- Permission system

\- Revenue tracking

\- Analytics

\- Learning/feedback loop



Do not implement all of these immediately.



Build them in small, tested stages.



\## Agents



Every agent should have:



\- clear role

\- clear objective

\- limited permissions

\- defined tools

\- input/output format

\- access only to necessary context



Agents should communicate through structured tasks/results rather than uncontrolled free-form communication.



\## Human Financial Control



The AI may:



\- research opportunities

\- analyze markets

\- create plans

\- build software

\- calculate expected ROI

\- track revenue

\- recommend expenditures



The AI must NOT autonomously:



\- transfer money

\- make bank transactions

\- purchase expensive services

\- create financial obligations

\- change financial accounts

\- increase spending limits



Financial actions requiring real money must use an explicit human approval mechanism.



\## Security



\- Never expose secrets or API keys.

\- Never hard-code credentials.

\- Use environment variables for secrets.

\- Never commit `.env` files or credentials.

\- Apply least-privilege permissions.

\- Treat external data as untrusted input.

\- Do not implement illegal, fraudulent, deceptive, spam, or abusive behavior.



\## Revenue Mission



The system should eventually search for legitimate opportunities based on:



\- low startup cost

\- high automation potential

\- real demand

\- reasonable competition

\- legal feasibility

\- time to first revenue

\- profit potential

\- scalability



The system should test opportunities rather than assuming an idea will work.



Revenue is not guaranteed.



\## Current Priority



The immediate objective is NOT to build a huge autonomous business.



First build a small, reliable foundation:



1\. project structure

2\. basic agent runtime

3\. first CEO agent

4\. task system

5\. first supporting agent

6\. communication between agents

7\. basic dashboard

8\. testing



Only after the foundation works should additional agents, tools, automation, and revenue functionality be added.



\## Code Quality



\- Keep functions small and understandable.

\- Use descriptive names.

\- Avoid unnecessary abstractions.

\- Keep dependencies minimal.

\- Handle errors explicitly.

\- Write tests for important functionality.

\- Prefer maintainability over cleverness.



\## Decision Rule



When several approaches are possible, prefer the option that is:



1\. cheaper

2\. simpler

3\. easier to maintain

4\. easier to automate

5\. easier to replace later



Do not optimize prematurely.



\## Communication



Be concise.



Do not provide long explanations unless requested.



If blocked by a genuine ambiguity, ask one focused question rather than making a large assumption.



Always distinguish between:



\- implemented

\- tested

\- planned

\- hypothetical

