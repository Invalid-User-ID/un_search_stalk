# SearchStalk

SearchStalk is a plugin for Nicotine+ that monitors incoming distributed search activity and logs matching searches based on configurable watch rules.

The plugin can track:

- Search terms (wildcard substring matching)
- Exact search queries
- Specific usernames

Logged hits are stored in JSONL format and can be viewed through lightweight local HTML interfaces.

## Features

- Wildcard (`W:`), exact (`E:`), and user (`U:`) watch rules
- JSONL append-only logging
- Local searchable web UI
- Wishlist-style repeated-query aggregation
- Automatic log cleanup and rotation
- Optional auto-add user tracking behaviors
- Deterministic match priority (`U > E > W`)
- Optional logging of all matching rules per search
- Threaded cleanup/rotation to reduce UI blocking
