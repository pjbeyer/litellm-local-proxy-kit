# Data model

| Entity | Owner and binding | Lifetime / failure |
|---|---|---|
| Policy and install receipt | Owner-provisioned policy; install-only utility records version, canonical policy digest, and controller source byte/mode facts | Changed bytes or policy require reconciliation; not independently authenticated history |
| Daemon incarnation and qualification | `serve` owns epoch; owner binds kernel identity, generated supervisor definition and matching canary evidence | New incarnation, expiry or drift invalidates the relevant binding |
| Prepared plan and approval | Controller records exact asset, runtime, original listener and deadlines; owner issues exact matching approval within the window | Consumed once, never replayed |
| Request, fence and child journal | Controller records service-control commitment, outcome and owned children | Uncertain records block; operator dispositions preserve evidence |

Fields and rules are owned by the [controller](contracts/controller.md), [shutdown](contracts/shutdown.md) and [reconciliation](contracts/reconciliation.md) contracts.
