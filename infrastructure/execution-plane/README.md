# Impact Preview S3 evidence

Deployed 2026-11-07 to the confirmed hackathon account `247495661854`, `us-east-1`:

- Stack: `intentlock-execution-plane-dev` (`CREATE_COMPLETE`)
- Bucket: `intentlock-evidence-247495661854-us-east-1`
- Private bucket: all four public-access blocks enabled, bucket-owner enforced, SSE-S3, versioning, TLS-only bucket policy, retained on stack deletion
- Scoped managed policies created for artifact writer (`PutObject`, `GetObject`) and reader (`GetObject`), limited to this bucket's `runs/*` objects. They are not attached to users or roles yet.

The bucket is separate from Ali's Lambda zip bucket. Do not upload `.env`, credentials, `.git`, or unrelated files. `S3Artifacts` accepts only the documented `runs/{run_id}/...` keys, limits patch/test/impact/audit objects to 2 MB and repository archives to 100 MB, uses a conditional `IfNoneMatch: *` write, sends a SHA-256 checksum, and rechecks the digest and metadata when reading. The control plane additionally checks patch bytes against the digest in the stored ImpactReport. A second patch candidate in one run gets its own test and impact object key. The input archive is built from the committed Git tree after rejecting protected and credential-like paths; it excludes untracked files and `.git`.

The local backend remains the fallback when `INTENTLOCK_ARTIFACT_BUCKET` is unset. To use S3, set `INTENTLOCK_ARTIFACT_BUCKET`, `INTENTLOCK_AWS_PROFILE`, and `AWS_REGION=us-east-1` in the Python processes running the execution plane, promoter, and local API. `apps/mcp-server/start_api_with_s3.py --person1-checkout /absolute/path/to/Ali-Person-1` starts Ali's API with the S3 reader injected through its existing `create_app(artifacts=...)` interface. Run it with the same API, reviewer, promoter, and launcher variables described in Ali's handoff. The API launcher needs `INTENTLOCK_AGENT_ENV_PASSTHROUGH=INTENTLOCK_ARTIFACT_BUCKET,INTENTLOCK_AWS_PROFILE` so Kiro's IntentLock MCP subprocess can write evidence. It never passes AWS credentials into the Docker sandbox. The local Kiro subprocess still runs under the user's HOME and is not an OS security boundary; use a dedicated least-privilege role/credential before treating this as production-ready.

The promoter reads the same bucket when `INTENTLOCK_ARTIFACT_BUCKET` is set:

```bash
INTENTLOCK_ARTIFACT_BUCKET=intentlock-evidence-247495661854-us-east-1 \
INTENTLOCK_AWS_PROFILE=hackathon AWS_REGION=us-east-1 \
python3 packages/patch-promoter/service.py --repository /absolute/path/to/demo-git-repo
```

The stack was validated using `aws cloudformation validate-template` before deployment. All 25 Person 2 tests passed after the archive/audit additions. A real S3 smoke put/read passed, and an opt-in full HTTP test passed with actual S3 objects: unsafe auth patch blocked, stopped run could not promote, safe patch reviewed and promoted. Real objects were verified under `runs/` for the input archive, patch, test output, impact report and final audit; the tests use disposable demo Git repositories and synthetic data. These checks used `AWS_PROFILE=hackathon` and `.venv/bin/python`.

Repeat the real S3 test explicitly:

```bash
INTENTLOCK_PERSON1_CHECKOUT=/absolute/path/to/Ali-Person-1 \
INTENTLOCK_ARTIFACT_BUCKET=intentlock-evidence-247495661854-us-east-1 \
INTENTLOCK_AWS_PROFILE=hackathon AWS_REGION=us-east-1 \
.venv/bin/python -B -m unittest discover -s tests/sandbox -p test_impact_preview_api.py -v
```

After a run reaches a terminal state, finalize its control-plane timeline (contract, run, events, impact reports, reviews and promotion records) with `.venv/bin/python -B packages/sandbox-provider/export_audit.py --run-id RUN_ID`. This writes the immutable `runs/RUN_ID/audit/final.json` object. Wait until the timeline is complete: a second export with changed contents will be refused rather than overwrite the audit. Ali's normal `create_app()` factory still defaults to its local artifact reader; the adapter above or an Ali-side reader selection is needed for the dashboard to serve S3 patches. No long-running API or promoter service was deployed to AWS; both remain local processes. The S3 bucket is live and retains its objects and versions. The existing local test can be run without AWS variables.
