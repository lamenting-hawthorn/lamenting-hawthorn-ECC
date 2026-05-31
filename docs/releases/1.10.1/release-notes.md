# ECC v1.10.1 Release Notes

## Positioning

ECC v1.10.1 is a stable-channel reliability patch.

This release keeps the public `latest` line moving while the larger ECC 2.0 control-plane, dedicated ECC agent, and agentic IDE work remains on the prerelease track.

## What Changed

- Hardened harness auditing around missing or malformed package metadata.
- Increased the continuous-learning observer default turn budget so the default 500-line analysis window has enough room to complete.
- Fixed `/instinct-status` plugin-root resolution so the command does not fall back to stale legacy install paths.
- Updated contribution and translated marketplace links to the current `affaan-m/ECC` public repo surface.

## ECC 2.0 Status

ECC 2.0 is still a prerelease lane.

The stable `1.10.x` line remains the right public install surface for users who want the current meta-harness, skills, rules, hooks, commands, and cross-harness install story without opting into the next control-plane generation.

The `2.0.0-rc` line is where the larger product vision belongs:

- meta-harness
- dedicated ECC agent
- control pane / agentic IDE
- multi-harness operating layer
- stronger evaluation, memory, and permission boundaries

## Upgrade

```bash
npm install -g ecc-universal@1.10.1
```

or:

```bash
npx ecc-universal@1.10.1 install --profile developer
```
