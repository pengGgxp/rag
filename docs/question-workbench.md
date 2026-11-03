# QW1: independent question workbench connector

Task 2026-11-03: make workbench access opt-in per user through site administration.
Add a versioned, audited user entitlement (default disabled); enforce it for login,
session introspection, consent, conversation selection and each model tool call.
Move the launch link from the account menu to product navigation for eligible
users. Preserve private question data and existing billing; retain user consent
for model writes. Verify admin-only changes, conflicts, revocation/re-enable,
session expiry, tool exclusion, backend regressions and responsive UI/build.
No independent model pricing, workbench business pages or new role system.

Access implementation: `PUT /site/admin/users/{id}/question-workbench` with
`enabled` and current `version`; edits are site-admin-only, versioned and audited.
Migration `0046_workbench_access` defaults all accounts to disabled. Enabling does
not bypass the user's model-write consent. Disabling clears consent in all their
workspaces and invalidates existing workbench identities/delegations; re-enabling
does not revive old sessions or consent. Questions, papers and billing are retained.

Validation: 11 connector/site administration checks and 75 conversation/Agent/
stream/context checks passed. A fresh pgvector PostgreSQL database migrated from
zero to head, and concurrent changes produced one success and one 409. Frontend
production build passed. Local browser verified the administrator switch and
eligible sidebar entry, with no account menu item; screenshots are in ignored output.

Administrator-access release deployed on 2026-11-03 at commit `f04e48c`.
API/Web images built successfully; the image-isolated connector and administrator
suite passed all 11 checks. Migration `0046_workbench_access` completed, and
pre/post-migration model prices, existing user fields, balances, ledger and consent
rows matched. Other service container IDs and both deployment environment files
remained unchanged. Backups and rollback images are recorded under
`/data1/rag-releases/qw-access-20261103`; the original request pause was restored.

Live synthetic-account acceptance verified default denial, no self-assignment,
administrator assignment, stale-version 409, real tool injection and Skill loading,
public HTTPS single sign-on, revocation of existing sessions/tools, and continued
invalidation after re-enabling. Test entitlements, sessions and temporary site-admin
access were removed afterward. No paid model or OCR call was made for this release.
Local browser layouts were inspected at 1440/700/390px. Existing accounts, including
administrators, start disabled; a site administrator explicitly enables each intended
user under Users -> User details -> Feature permissions -> Question workbench.

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
Keep the task open until unverified evidence is explicitly resolved.

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
app's docs/verification-QW1.md. That verification was local.

Online release on 2026-11-03: `https://q.str1ct.top` is live with a dedicated
database, file volume and export Worker. RAG migrated to
`0045_question_workbench`; only API/Web were recreated. Existing Worker, database,
Redis, MinIO and executor container IDs and the original `.env` stayed unchanged.
Pre/post-migration snapshots of prices, workspaces, balances, ledger and chat
content matched. Backups and rollback images are recorded under
`/data1/rag-releases/qw1-20261103`. The original request pause state was restored.

Image-isolated regression: 66 RAG checks passed; 14 workbench checks passed with
2 skipped; independent local PostgreSQL migration/concurrency checks: 2 passed.
Both production Web images built successfully. A dedicated synthetic account used
normal signup credit and normal billing with `deepseek-flash`, actually loading
the Skill, saving a question, creating a paper and exporting Word. Public HTTPS
SSO, secure cookies, authenticated Word/PDF downloads and anonymous 401 passed.
Browser navigation in both directions and 1440/700/390px list layouts were checked;
the downloaded Chinese PDF was rendered and inspected. No OCR or private user
content was used. Full evidence and desktop Word/WPS and online scenario limits
remain in the independent app's verification document.
