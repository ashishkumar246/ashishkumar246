# Yakkamon OpenSea ↔ Ronin mismatch watcher

This watcher looks specifically for **active OpenSea listings that OpenSea currently tags as `Status = Bad Egg`**, then checks the exact same token ID against the NFT's live `tokenURI` through Ronin RPC.

A token is written to `mismatches.json` only when:

1. OpenSea returns it as an active Bad Egg listing; and
2. its live Ronin-chain metadata can be fetched successfully; and
3. the live metadata's `Status` is not `Bad Egg`.

Collection contract:

`0x6d1bc5247ca99d917d91ec52dbbb5ef6c2435107`

The workflow runs every 30 minutes. It creates a temporary free OpenSea API key at runtime, so no OpenSea secret needs to be stored in GitHub.

## Output

`mismatches.json` contains the current verified mismatch set, OpenSea price when parseable, both marketplace URLs, and any verification errors.

Important: "not Bad Egg in live metadata" means the current metadata does not contain `Status: Bad Egg`. It is not a guarantee that the project cannot later change metadata/status.
