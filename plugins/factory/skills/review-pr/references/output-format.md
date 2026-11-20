# Output formats for /factory:review-pr

## Default: Markdown report

Print this and nothing else after it. Leave out a severity section that has no findings. With no findings at all, print the header, `No findings.`, and the summary.

```markdown
## Review: <repo> <#pr or base..head short SHAs>
Ticket: <KEY or none> · Base: <base short SHA> · Head: <head short SHA>
Standards: CLAUDE.md <read|missing> · REVIEW.md <read|missing> · Rules: <comma-separated paths or none>

### Important (<count>)
- **F1** `path/to/file.py:42` <title>
  <body, indented two spaces; ends with the concrete failing input or state and the smallest fix>
  Rule: <rule, only when set>

### Nit (<count>)
- **F2** `path/to/file.py:88-91` <title>
  <body>

### Pre-existing (<count>)
- **F3** `path/other.py:10` <title>
  <body>

### Summary
<two or three sentences: what the change does and the verdict>
<only with --comment: "Posted: <review html_url>" or "Not posted: <reason>">
```

Use `file:line-end_line` when `end_line` is set.

## --json: review-result object

Rules:
- The final message is the raw object. No code fences, no text before or after it.
- Top-level keys allowed: `repo`, `pr`, `ticket`, `base`, `head`, `standards`, `findings`, `summary`, `posted`. No others (`additionalProperties` is false).
- Required: `repo`, `base`, `head`, `standards`, `findings`, `summary`. Always also write `pr`, `ticket` and `posted`.
- `repo`: always matches `^[^/]+/[^/]+$`. `owner/name`; when there is no GitHub remote, `local/<repo directory name>`; on a fatal error before the repo is known, `local/unknown`. Never a bare `local`.
- `pr`: integer, or `null` outside PR mode. `ticket`: Jira key string or `null`.
- `base`, `head`: full 40-character SHAs.
- `standards`: `{"claude_md": bool, "review_md": bool, "rules": [paths]}`. No other keys.
- `findings`: array, possibly empty. Each finding has exactly these keys: `id` (`F1`, `F2`, ...), `severity` (`Important` | `Nit` | `Pre-existing`), `file`, `line` (integer >= 1), optional `end_line` (integer > line; omit it when the finding is one line, never write `end_line` equal to `line`), `title` (1 to 120 characters), `body` (non-empty), optional `rule` (string). Omit `end_line` and `rule` rather than writing `null`.
- At most 5 findings with severity `Nit`.
- `summary`: two or three sentences. When REVIEW.md is missing it starts with `No REVIEW.md found; reviewed against CLAUDE.md and the factory defaults.`
- `posted`: `true` only when the COMMENT review was created in this run; otherwise `false`. If posting failed, add one sentence to the summary saying why.
- Escape newlines inside strings as `\n`; the object must parse with a strict JSON parser.

Example (synthetic):

{"repo":"acme/widgets","pr":17,"ticket":"WID-204","base":"3f1c2a9d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39","head":"9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a07","standards":{"claude_md":true,"review_md":true,"rules":[".claude/rules/api.md"]},"findings":[{"id":"F1","severity":"Important","file":"src/widgets/pricing.py","line":58,"end_line":61,"title":"Discount applied twice when a coupon and a bulk tier both match","body":"`apply_discounts()` now runs `bulk_discount()` after `coupon_discount()`, and both read `order.subtotal` instead of the running total. For a 100.00 order with a 10% coupon and a 10% bulk tier the total is 80.00, not 81.00. Pass the running total to the second call.","rule":"Money math uses the running total"},{"id":"F2","severity":"Nit","file":"src/widgets/pricing.py","line":12,"title":"Unused import of Decimal","body":"`Decimal` is imported but no longer used after this change. Remove the import."},{"id":"F3","severity":"Pre-existing","file":"src/widgets/api.py","line":140,"title":"Missing permission check on widget export","body":"`export_widgets()` returns every tenant's widgets to any logged-in user; a user of tenant A can call `/export?tenant=B`. This change does not touch it. Check `request.user.tenant_id` against the query parameter."}],"summary":"Adds bulk-tier discounts to checkout pricing. One Important bug: discounts compound on the subtotal, so orders with a coupon and a bulk tier are undercharged. Also one Nit and one Pre-existing permission gap for a Jira ticket.","posted":false}
