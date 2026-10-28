> Status (2026-10-24): Public comments are allowed only for confident, complete summaries, and the comment body must contain only the video summary itself. Processing progress, retries, errors, evidence-source notes, subtitle checks, DM content, and visibility receipts must stay private.
>
> Confidence gate: Public comments are allowed only for confident, complete summaries. Low-confidence, incomplete, unclear, or subtitle/image-mismatch cases are private-message only and must never post a public comment.

# Bilibili Comment @ Delivery Module

## Scope

This module is intentionally isolated from the existing QQ, private-message,
admin-panel, and video-processing code. It only provides:

1. Main-site Bilibili cookie loading from `storage_state.json`.
2. BVID-to-aid resolution.
3. Deterministic comment text with a leading `@` mention.
4. A safety-gated comment-post call.
5. A read-only reply-list lookup for visibility verification.

## Safety Contract

- `send_comment()` defaults to `execute=False` and only returns a `dry_run` preview.
- A real send is accepted only when all of these are true:
  - HTTP status is 2xx.
  - API `code` is exactly `0`.
  - The response contains a positive `rpid`.
- `accepted=True` means the API accepted the request. It does not prove public visibility.
- `verify_comment_visible()` is read-only and reports `not_found_on_page` when the
  comment is absent from the requested page. That result must not be interpreted
  as proof that the comment was never posted.
- Cookie values and CSRF tokens are never included in returned previews or errors.

## Interface Assumptions

1. BVID lookup:
   - `GET https://api.bilibili.com/x/web-interface/view?bvid={bvid}`
   - Requires `code=0` and positive `data.aid`.
2. Comment creation:
   - `POST https://api.bilibili.com/x/v2/reply/add`
   - Form fields: `type=1`, `oid=aid`, `message`, `plat=1`, `csrf=bili_jct`.
   - Optional mention map: `at_name_to_mid={"NAME":"UID"}`.
   - Expected response fields: `data.rpid`, `data.rpid_str`, optional `msg_key`.
3. Read-only verification:
   - `GET https://api.bilibili.com/x/v2/reply`
   - Parameters: `type=1`, `oid=aid`, `sort`, `pn`, `ps`.
   - Searches `data.replies` and `data.top_replies`, including nested replies.

## Unverified Points

- No real comment was sent during implementation.
- The exact `at_name_to_mid` format and whether this endpoint currently uses it
  have not been verified against the live Bilibili web client.
- A new comment may enter moderation or be invisible to some users even when an
  `rpid` is returned.
- Readback checks only the requested page. Pagination or ranking can make a new
  comment absent from page one.
- Bilibili may later require extra device parameters, WBI signing, CAPTCHA, or a
  higher account level.
- `msg_key` may not exist in the comment API response; an empty value does not
  change the `rpid` success rule.

## Suggested Integration

```python
from bili_comment_delivery import BilibiliCommentClient

client = BilibiliCommentClient()

# Preview only. No POST request is made.
preview = client.send_comment(
    bvid="BV1xx411c7mD",
    body="One-line conclusion from the video summary",
)

# The business layer should call this only after explicit approval.
sent = client.send_comment(
    bvid="BV1xx411c7mD",
    body="One-line conclusion from the video summary",
    execute=True,
)

if sent["accepted"]:
    checked = client.verify_comment_visible("BV1xx411c7mD", sent["rpid"])
    if checked["verified"]:
        pass  # Only here may the workflow report that the comment is visible.
```

Integration rules:

- Deduplicate by BVID using the existing task store.
- Never deliver when the summary is empty, the task failed, or the item was skipped.
- Generate only the body with the model; prepend the mention deterministically.
- Persist `rpid` and the readback result, not just the initial acceptance state.
- Treat send and visibility check as separate task states.
