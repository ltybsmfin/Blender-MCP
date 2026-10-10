# Tool compatibility fixture

legacy90_tools.json was captured from the production build_server(profile="all")
before the dynamic-domain migration on 2026-10-05. It contains all original 90
names, descriptions, input/output schemas and tool metadata. Dynamic-domain
regressions compare the complete definitions to it; only the three new domain
control tools may be added. Do not regenerate it from the new implementation
to make a failed compatibility check pass.
