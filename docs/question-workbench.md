# QW1: independent question workbench connector

The approved app lives in E:/project/question-workbench. RAG retains document
permissions, model orchestration and billing; question/paper data stays in the new
app's separate database. No old education project is imported or deployed.

Implement a fixed first-party app bridge: one-use login codes, server-side session
introspection, explicit user/workspace grants and per-conversation selection. Tools
never accept owner IDs, secrets or arbitrary endpoints. No deletion tools. Login
codes expire in 60 seconds; tool delegations expire in 60 seconds. Existing account,
workspace, CSRF and model billing semantics remain in force.

Acceptance: private questions -> paper snapshots -> real DOCX/PDF; safe login and
revocation; visible ordered activities; refresh and slash/picker selection; isolated
tests, PostgreSQL migrations, responsive screenshots and real-model verification.
Keep the task open until unverified evidence is explicitly resolved. Local only.

Deployment authorized on 2026-11-03 at `https://q.str1ct.top`, superseding the
local-only delivery boundary. Inspect active container Compose labels, back up the
live database/source/configuration/images, deploy the standalone workbench and
update only RAG API/Web plus the connector migration. The bridge override uses a
dedicated connection secret. Preserve prices, balances, existing grants and RAG
data services; record actual online acceptance before declaring completion.

UI follow-up (2026-11-03): remove browser-default link underlines inside the
conversation extension picker. Keep link color, keyboard focus and selection
behavior. Verify the computed link style in the running local chat; do not change
document citations or the workbench authorization flow.

Local verification on 2026-11-02: full backend regression 694 passed / 49 skipped;
final connector and Agent checks 30 passed. A real model loaded the authoring Skill,
retrieved a synthetic source, saved two sourced questions, assembled a paper and
completed a downloadable DOCX. Lost creation responses are reconciled by querying
the original operation result, without repeating a write. Detailed evidence and
remaining desktop Word/WPS and interruption limits are recorded in the independent
app's docs/verification-QW1.md. No production deployment was performed.
