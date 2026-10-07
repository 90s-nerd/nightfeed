# Nightfeed agent evaluations

The automated tests check routing, context retention, tools and approvals using
fixtures. They cannot establish how a particular hosted model interprets the
instructions. Run this opt-in suite when changing the policy or configured model:

```powershell
$env:NIGHTFEED_EVAL_API_KEY = '<provider key>'
python tests/evals/check_agent.py --config provider.json --output results.json
```

`provider.json` uses the same non-secret connection fields as Nightfeed:
`api_type` (`compatible`, `responses`, `anthropic` or `gemini`), `base_url`,
`model`, `timeout`, `max_tokens`, and optionally `name`. Keep the key in the
environment, not that file. Requests incur your provider's usual charges. No
tools execute, no production database is read, and no feeds or tasks change.

The suite uses synthetic histories and checks the first tool choice and critical
filters. Review the saved prose for scope refusals, groundedness, friendly persona
and specific clarifications; a no-tool response alone does not prove a correct
refusal. Results include the policy version and model for comparison between runs.
Do not publish provider credentials or private evaluation output.
