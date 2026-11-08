# Demo and evaluation resources

These files support running and verifying Fonecta Connect. All business records
are synthetic. No API keys or real customer records are included.

| Directory | Used for |
|---|---|
| `data/` | Business records, opening-hours history and context consumed by the demo and model. |
| `mock_api/` | Local Yext simulator used when real sandbox access is not configured. |
| `examples/` | Shared hours, message and API helpers imported by the application. |
| `evaluation/` | Target dates and the supplied prediction evaluator. |
| `labels/` | Simulator outcomes and optional development-score checks; never read by model inference. |

For setup and launch commands, use the [project README](../README.md).

Attribution: **Connect Suggests simulator, Since AI ry 2026**. Supplied code is
MIT licensed; supplied synthetic data is CC BY 4.0. The original notice is
preserved in [LICENSE.md](LICENSE.md). Model/data fingerprints require these
input files to retain their original bytes.
