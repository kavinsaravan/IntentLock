# IntentLock execution-plane boundaries

This increment controls only the advertised MCP read tool. It does not yet claim
the complete TwinGuard or live Kiro stopped-run demo. The stop lifecycle is verified
with a real subprocess against the actual local control-plane API. The Kiro configuration is an application
restriction, not an OS sandbox for the Kiro process. Global Kiro context/settings
must be inspected in the live integration test before claiming all data access
passes through the gateway. Launching outside the protected repository reduces
implicit repository-context loading.

The read adapter opens path components relative to directory descriptors with
O_NOFOLLOW. Protected path checks are defense in depth; Person 1's authorizer
owns mission-specific allowed/protected paths. Repository root and launch identity
are trusted configuration. The host and authorization service are trusted.

Authorization requests include requested paths; tool contents are never sent to
the authorization service or logged by this adapter. Error responses omit host
exception details. Authorization service authentication and HTTPS deployment are
integration requirements. This is a local stdio adapter, not AgentCore Gateway.

Future patch evaluation must preserve trusted tests outside the writable agent
snapshot, disable network access and host credentials, bound all resources, and
promote only the reviewed patch after verifying its hash and clean base state.
The same Git commit alone does not prove an unchanged working tree.
