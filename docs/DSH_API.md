# The dsh API: what this package relies on

## Two facts to know before you build on this

**A blocked worker is indistinguishable from a finished one.** The `TurnEndReasonMap` of `dsh` is `completed | aborted | blocked | error | max-tokens | interrupted`. There is no input-required state. A worker that asked a question and waits ends its turn as `completed` and sits `idle`. The signal is on a different plane: an answerable server-request frame on the mux stream. `dsh_pending` reads it. Check it whenever a worker goes idle early.

**The event streams are WebSocket, not SSE.** `/api/events.mux` and `/api/events.host` answer a plain GET with `426 upgrade required`. The `dsh` source also contains an SSE carrier for the same API, but it belongs to a different deployment. That mistake cost a full working implementation that read zero frames.

The skill covers both facts in more detail, with source citations.

## Tested against dsh

The dsh API is not a documented, stable interface. This package uses it through three calls (`DshClient` in `transport.py`: `rpc`, `post`, `stream`). The package was tested against dsh package version `0.1.1-rc.2`, commit `b150a55`. `contracts/dsh-shapes.json` records the keys and types of real responses from that server. It holds no values.

dsh reports a version in `host.describe`, but it is the constant `0.0.1`, so the tool cannot use it. The tool checks the shape of responses instead. `shape.REQUIRED` lists the fields that the code reads. The server and `dshctl` check the first responses of each method. If a field is missing or has another type, the call still returns. `dsh_brief` then adds a `dshShape` list with the problem, and a line in `needsAction` that tells the lead to report it. `dshctl` prints a warning on stderr.

To record the shapes of another dsh, run `uv run python scripts/record-shapes.py` against it. The script makes read-only calls. A test checks that `shape.REQUIRED` fits the recording and fits the fake that the tests use. The recording does not cover `session.create`, `session.fork`, `session.rename` and `session.cancel`, because they change state. Those four are checked against the fake only. The frames of the pending-requests stream and the two POST endpoints (`respond`, `commands/execute`) have no shape check.

## What ran against a live dsh

We exercised the original tools against a live `dsh` server. Verified behaviours worth knowing: the block and unblock loop works **unattended**, with no browser or UI client in the path. A title that you set explicitly **pins**, and it survives later turns. `dsh_cancel` tells a real abort from a lost race.

We first tested `dsh_digest`, `dsh_brief`, the usage record on tasks and the `compactionStalled` flag only against a fake server. They have since run in the live project of the author, with earlier versions of this code. The state directory of that project holds usage records on tasks and the `compactionStalled` flag on workers, and its call log has `dsh_digest` and `dsh_brief` calls. We did not check their output against the dsh events by hand.

[Status in the README](../README.md#status) lists what has not run live since the client interface was added.

## Provenance

We checked the transport claims in this repo against a running server and the `dsh` source when we wrote them. We did not infer them from documentation. The code began as throwaway scripts in an evaluation workspace, and we extracted it from there.
