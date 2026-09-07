# Web source provenance

## Access record

- Accessed: 2026-09-01 (America/New_York).
- Pull request: https://github.com/p-e-w/heretic/pull/211
- Pull-request title: `feat: Arbitrary-Rank Ablation (ARA)`.
- Pull-request status at access: open draft, not merged, 19 commits.
- Pull-request head: `edc3b123456c7f86f24d409b838ab3a7226e285e` (`origin/ara`).
- Pull-request base reported by the GitHub API during planning: `96c7a7d98a4710903355f398cfd0ff1c65f55925`.
- ArXiv record: https://arxiv.org/abs/2511.08379 (v2, revised 2025-11-13).
- Proceedings record: https://ojs.aaai.org/index.php/AAAI/article/view/40551
- Proceedings DOI: https://doi.org/10.1609/aaai.v40i39.40551
- Proceedings publication date shown by the official page: 2026-03-14.

## Claim classes

| Source | Directly supports | Does not independently support |
| --- | --- | --- |
| Pull request and fixed source revision | implementation control flow, tensors, losses, optimizer settings, open/draft status, author claims and public discussion | peer-reviewed effectiveness, general convergence, fair comparison against published baselines |
| ArXiv v2 and AAAI proceedings | SOM formulation, reported setup and peer-reviewed empirical conclusions | performance of ARA or a head-to-head experiment with the draft implementation |

## Use rule

The paper must label source-code observations as implementation facts, pull-request prose and discussion as developer claims, and proceedings results as reported empirical evidence. It must not combine these categories into a new numerical comparison.
