# Cloud computer agent guidance

Goal: make the agent use the verified cloud environment to complete file and
HTTP preview tasks, inspect existing inputs, repair ordinary execution errors,
and verify real outputs before delivery.

Scope: system instructions, file/preview tool descriptions, and a server-owned
default-template capability description preserved during context rebuilding.
No execution gates, permission changes, billing changes, or deployment.

Acceptance: simulated agent requests receive the same environment information
before and after context rebuilding; unknown/disabled templates do not claim the
verified template's packages. Run focused agent, context, and file-tool tests.
No real model calls.

Completed: behavior guidance now covers inspecting inputs, installing ordinary
dependencies, reopening generated documents, checking available rendering,
repairing execution errors, and reporting verified output/preview links. Tool
descriptions distinguish binary paths from visual understanding. The runtime
injects the verified default-template description into initial and rebuilt
context; other templates get no package claims. Existing environments are
explicitly allowed to differ from the new default.

Validation: 66 tests passed across cloud environment, agent, context budget,
HTTP preview, cloud file tools, and personal extensions. Diff whitespace checks
passed. No real model calls or deployment performed for this change. Real model
behavior under the revised instructions remains unverified.
