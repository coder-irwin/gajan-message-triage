# How AI was used

The brief asks which AI tools I used and for what. This is the full account. The decision log has the short version.

## Tools

| Tool | Used for |
|---|---|
| **Claude Code**, Anthropic's coding agent, on Claude Opus 5.5 | Designing and writing the code, tests, web app, Dockerfile, README and decision log. Running and checking everything listed below. |
| **Gemini** (`gemini-3.5-flash-lite`, `gemini-2.5-flash-lite`) | The model inside the product, which classifies each message. Not used to write code. |
| A general-purpose chat assistant | Before this task, to summarise my background and prepare for the interview. Not used for the code. |

## What I decided

- Use Gemini as the provider, and the cheapest model that works.
- Run messages through a pipeline with one model call each, not an autonomous agent.
- Make the app bring-your-own-key, with no credentials of its own.
- Ask for an observation report of a full run, screenshots of a real run, and a manual check of every result before submission.
- Supply the Google Cloud project used for the live runs.

## What Claude Code built

- The pipeline: record repair, regex signals and risk flags, the Gemini and Claude classifiers, the offline rules fallback, the policy layer with the human-review rule, and the per-message trace.
- The observation report, the web app, the HTTP API, and the Dockerfile.
- 30 tests, plus two hostile input files.
- First drafts of the README and decision log.

## What Claude Code reviewed and verified

Each check below was actually run in this session, and each finding was fixed.

| Check | What it found | Fix |
|---|---|---|
| Read all 25 live results one by one | The model invented facts in reply drafts, such as "Yes, we are open on Sundays" and "I have initiated the refund" | Automated replies became code templates. Agent drafts are scanned and promises are flagged. |
| Compared the cost summary with the raw token counts | Calls answered from Google's cache were not counted, which inflated cost per 1,000 | Count calls by classifier instead of by input tokens |
| Tested the draft warnings | The warning patterns had been corrupted while editing and could never match | Rewrote them and scanned every file for stray control characters |
| Ran a hostile input file | Unknown brands landed in made-up queues, and numeric timestamps were rejected | A shared holding queue, and timestamp conversion |
| Ran the same model four times | Risky messages routed identically every time. Some routine ones did not: MSG-022's priority, MSG-020's queue and whether MSG-003 was automated changed between runs. An earlier claim that routing was stable was wrong and has been corrected. | Documented in the decision log, with a fix proposed for another day |
| Checked README claims against the data | The prompt size, one cost claim and the list of automated messages were wrong | Corrected with measured numbers |
| Followed the README from a clean clone | Installs, all tests pass, offline and live runs work | None needed |
| Bring-your-own-key safety | A fake key never appeared in output, the page or the server log. A key in the server's environment is never used for a caller who brought none. | Added a test for the second point |
| Built and ran the Docker image | The project option is correctly off in the container, and offline runs work | The page now hides that option when it is off |
| Looked at every screenshot | All ten render correctly and match the run data | None needed |
| Scanned the repo and zip for secrets | No keys present. `.env` and outputs are git-ignored. | None needed |
| Checked model availability and prices | 2.5 Flash-Lite is closed to new API keys. The pasted AI Studio key had no credits. | 3.5 Flash-Lite is the default, and live runs went through Vertex AI |

## What was not verified

- **A funded Gemini API key, end to end.** The key path was tested only with invalid and fake keys. Live runs used a Google Cloud project instead.
- **The Claude provider.** It is covered by tests with a fake client but was never run live, because there was no Anthropic key.
- **An actual Cloud Run deployment.** The container was built and run locally only.
- **Accuracy on real traffic.** There is no labelled data beyond these 25 messages.
